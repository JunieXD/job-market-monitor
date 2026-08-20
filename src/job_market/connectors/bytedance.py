import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.browser_json import (
    JsonResponseQueue,
    drain_json_responses,
    enqueue_json_response,
    next_json_payload,
)
from job_market.raw_store import RawStore
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

API_CAP = 10_000
UI_PAGE_SIZE = 200
RESPONSE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Partition:
    label: str
    category_ids: tuple[str, ...] = ()
    location_codes: tuple[str, ...] = ()
    category_name: str | None = None
    location_name: str | None = None


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    page_url: str
    portal_type: int
    website_path: str
    portal_channel: str


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        page_url="https://jobs.bytedance.com/experienced/position",
        portal_type=2,
        website_path="society",
        portal_channel="office",
    ),
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        page_url="https://jobs.bytedance.com/campus/position",
        portal_type=3,
        website_path="campus",
        portal_channel="campus",
    ),
}


class ByteDanceConnector:
    """Collect ByteDance's public career JSON through a real browser context."""

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._search_responses: JsonResponseQueue = asyncio.Queue()
        self._filter_responses: JsonResponseQueue = asyncio.Queue()
        self._filters: dict[Channel, dict[str, Any]] = {}
        self._partition_counts: dict[Partition, int] = {}
        self._current_partition: Partition | None = None
        self._current_payload: dict[str, Any] | None = None
        self._active_portal: PortalConfig | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS[channel]
        root = Partition(label="all")
        filters, _ = await self._open_partition(portal, root)
        partitions = await self._build_partitions(portal, filters)
        jobs_by_id: dict[str, JobRecord] = {}
        partition_counts: dict[str, int] = {}
        complete = True

        for partition in partitions:
            count = await self._count_partition(portal, partition)
            partition_counts[partition.label] = count
            if self._current_partition == partition and self._current_payload is not None:
                payload = self._current_payload
            else:
                _, payload = await self._open_partition(portal, partition)
            offset = 0
            while offset < count:
                if max_pages is not None and self.pages_fetched >= max_pages:
                    complete = False
                    break

                self.pages_fetched += 1
                if self.raw_store is not None:
                    self.snapshots.append(
                        self.raw_store.save(
                            channel=channel,
                            partition=partition.label,
                            offset=offset,
                            payload=payload,
                        )
                    )

                rows = payload.get("data", {}).get("job_post_list", [])
                if not rows:
                    raise RuntimeError(
                        f"Empty non-terminal page: channel={channel} partition={partition.label} "
                        f"offset={offset} count={count}"
                    )
                for row in rows:
                    record = self.parse_job(row, channel)
                    jobs_by_id[record.external_id] = record

                offset += len(rows)
                if offset < count:
                    payload = await self._next_page(partition)
            if not complete:
                break

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _count_partition(self, portal: PortalConfig, partition: Partition) -> int:
        cached = self._partition_counts.get(partition)
        if cached is not None:
            return cached
        _, response = await self._open_partition(portal, partition)
        data = response["data"]
        count = data.get("count")
        if not isinstance(count, int) or count < 0:
            raise RuntimeError(f"Invalid count for partition {partition.label}: {count!r}")
        self._partition_counts[partition] = count
        return count

    async def _open_partition(
        self,
        portal: PortalConfig,
        partition: Partition,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._active_portal is None:
            filters, payload = await self._initialize_portal(portal)
            self._filters[portal.channel] = filters
            self._active_portal = portal
            self._set_current_partition(Partition(label="all"), payload)
        elif self._active_portal != portal:
            raise RuntimeError("ByteDance connector cannot switch portals in one session")
        elif self._current_partition == partition and self._current_payload is not None:
            return self._filters[portal.channel], self._current_payload
        else:
            payload = await self._reset_to_root()

        if partition.category_name:
            payload = await self._select_category(partition)

        if partition.location_name:
            payload = await self._select_location(partition)

        self._set_current_partition(partition, payload)
        return self._filters[portal.channel], payload

    async def _initialize_portal(
        self,
        portal: PortalConfig,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        await self._rate_limit()
        self._drain_response_queues()
        list_url = f"{portal.page_url}?current=1&limit={UI_PAGE_SIZE}"
        await self.page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
        payload = await self._next_search_payload("jobs:all:root")
        filters_payload = await self._next_filter_payload(portal)
        await self._dismiss_optional_overlays()
        return filters_payload["data"], payload

    async def _reset_to_root(self) -> dict[str, Any]:
        root = Partition(label="all")
        if self._current_partition == root and self._current_payload is not None:
            return self._current_payload
        await self._rate_limit()
        self._drain_queue(self._search_responses)
        clear = self.page.locator("span.filterClear:not(.disabled)")
        if await clear.count() != 1:
            raise RuntimeError("Cannot uniquely locate the active ByteDance filter reset")
        await clear.click()
        payload = await self._next_search_payload("jobs:all:reset")
        self._set_current_partition(root, payload)
        return payload

    async def _select_category(self, partition: Partition) -> dict[str, Any]:
        if partition.category_name is None:
            raise RuntimeError(f"Partition has no category name: {partition.label}")
        await self._rate_limit()
        self._drain_queue(self._search_responses)
        label_selector = (
            "span.atsx-clamp-content[data-cy-value="
            f"{json.dumps(partition.category_name, ensure_ascii=False)}]"
        )
        category = self.page.locator(f"li:has({label_selector}) > span.atsx-tree-checkbox")
        if await category.count() != 1:
            raise RuntimeError(f"Cannot uniquely locate category filter: {partition.category_name}")
        await category.click()
        return await self._next_search_payload(f"jobs:{partition.label}:category")

    async def _select_location(self, partition: Partition) -> dict[str, Any]:
        if partition.location_name is None:
            raise RuntimeError(f"Partition has no location name: {partition.label}")
        await self._rate_limit()
        self._drain_queue(self._search_responses)
        city = self.page.locator(".filter-ellipsis-text-item").filter(
            has_text=partition.location_name
        )
        if await city.count() != 1:
            raise RuntimeError(f"Cannot uniquely locate city filter: {partition.location_name}")
        await city.click()
        return await self._next_search_payload(f"jobs:{partition.label}:city")

    def _set_current_partition(
        self,
        partition: Partition,
        payload: dict[str, Any],
    ) -> None:
        count = payload["data"].get("count")
        if not isinstance(count, int) or count < 0:
            raise RuntimeError(f"Invalid count for partition {partition.label}: {count!r}")
        self._partition_counts[partition] = count
        self._current_partition = partition
        self._current_payload = payload

    async def _next_page(self, partition: Partition) -> dict[str, Any]:
        await self._rate_limit()
        await self._dismiss_optional_overlays()
        self._drain_queue(self._search_responses)
        next_page = self.page.locator("li[title='下一页']")
        if await next_page.get_attribute("aria-disabled") == "true":
            raise RuntimeError(
                f"Pagination ended before declared count for partition {partition.label}"
            )
        await next_page.click()
        payload = await self._next_search_payload(f"jobs:{partition.label}:next")
        self._current_payload = payload
        return payload

    async def _dismiss_optional_overlays(self) -> None:
        close_guide = self.page.get_by_role("button", name="关闭引导")
        if await close_guide.count() and await close_guide.first.is_visible():
            await close_guide.first.click()
            await self.page.locator(".hire-ai-assistant-guide-cn-mask").wait_for(
                state="hidden", timeout=5_000
            )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if "/api/v1/search/job/posts" in response.url:
            enqueue_json_response(self._search_responses, response)
        elif "/api/v1/config/job/filters/" in response.url:
            enqueue_json_response(self._filter_responses, response)

    async def _next_search_payload(self, operation: str) -> dict[str, Any]:
        payload = await self._next_response(self._search_responses, operation)
        self._last_request_at = asyncio.get_running_loop().time()
        return self._validate_response(payload, operation)

    async def _next_filter_payload(self, portal: PortalConfig) -> dict[str, Any]:
        operation = f"filters:{portal.channel.value}"
        payload = await self._next_response(self._filter_responses, operation)
        return self._validate_response(payload, operation)

    @staticmethod
    async def _next_response(queue: JsonResponseQueue, operation: str) -> dict[str, Any]:
        return await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"ByteDance page response: {operation}",
        )

    async def _rate_limit(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self.settings.bytedance_request_delay_seconds - (
            loop.time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _drain_response_queues(self) -> None:
        self._drain_queue(self._search_responses)
        self._drain_queue(self._filter_responses)

    @staticmethod
    def _drain_queue(queue: JsonResponseQueue) -> None:
        drain_json_responses(queue)

    async def _build_partitions(
        self,
        portal: PortalConfig,
        filters: dict[str, Any],
    ) -> list[Partition]:
        root = Partition(label="all")
        root_count = await self._count_partition(portal, root)
        if root_count < API_CAP:
            return [root]

        categories = filters.get("job_type_list") or []
        if not categories:
            raise RuntimeError("The source reached its cap but exposed no category partitions")

        partitions: list[Partition] = []
        for category in categories:
            category_id = str(category["id"])
            category_name = str(category.get("name") or category_id)
            candidate = Partition(
                label=f"category-{category_name}",
                category_ids=(category_id,),
                category_name=category_name,
            )
            category_count = await self._count_partition(portal, candidate)
            if category_count < API_CAP:
                partitions.append(candidate)
                continue
            partitions.extend(
                await self._split_large_category(
                    portal,
                    filters,
                    category,
                    category_name,
                )
            )
        if not partitions:
            raise RuntimeError("No non-empty partitions were produced")
        return partitions

    async def _split_large_category(
        self,
        portal: PortalConfig,
        filters: dict[str, Any],
        category: dict[str, Any],
        category_name: str,
    ) -> list[Partition]:
        cities = filters.get("city_list") or []
        if cities:
            city_partitions: list[Partition] = []
            for city in cities:
                city_code = str(city["code"])
                city_name = str(city.get("name") or city_code)
                candidate = Partition(
                    label=f"category-{category_name}-city-{city_name}",
                    category_ids=(str(category["id"]),),
                    location_codes=(city_code,),
                    category_name=category_name,
                    location_name=city_name,
                )
                count = await self._count_partition(portal, candidate)
                if count >= API_CAP:
                    raise RuntimeError(
                        "A category/city partition still reaches the API cap; "
                        "add another source-specific partition dimension before collecting."
                    )
                if count:
                    city_partitions.append(candidate)
            if city_partitions:
                return city_partitions

        children = category.get("children") or []
        if children:
            return [
                Partition(
                    label=f"category-{category_name}-{child.get('name', child['id'])}",
                    category_ids=(str(child["id"]),),
                    category_name=str(child.get("name") or child["id"]),
                )
                for child in children
            ]
        raise RuntimeError(f"Cannot split capped category: {category_name}")

    @staticmethod
    def _validate_response(response: dict[str, Any], operation: str) -> dict[str, Any]:
        if response.get("code") != 0 or not isinstance(response.get("data"), dict):
            message = json.dumps(response)[:1000]
            raise RuntimeError(f"Invalid ByteDance {operation} response: {message}")
        return response

    @staticmethod
    def parse_job(raw: dict[str, Any], channel: Channel) -> JobRecord:
        category = raw.get("job_category") or {}
        parent = category.get("parent") or {}
        recruit_type = raw.get("recruit_type") or {}
        subject = raw.get("job_subject") or {}
        subject_name = subject.get("name") if isinstance(subject, dict) else None
        if isinstance(subject_name, dict):
            subject_name = subject_name.get("i18n") or subject_name.get("zh_cn")

        info = raw.get("job_post_info") or {}
        addresses = info.get("address_list") or []
        cities = raw.get("city_list") or []
        locations = ByteDanceConnector._parse_locations(cities, addresses)
        if not locations:
            raise ValueError(f"ByteDance job has no location: {raw.get('id')}")

        published_ms = raw.get("publish_time")
        if not isinstance(published_ms, (int, float)):
            raise ValueError(f"ByteDance job has invalid publish_time: {raw.get('id')}")

        category_id = str(category.get("id") or "").strip()
        category_name = str(category.get("name") or "").strip()
        if not category_id or not category_name:
            raise ValueError(f"ByteDance job has no direct category: {raw.get('id')}")
        parent_id = str(parent.get("id") or "").strip() or None
        parent_name = str(parent.get("name") or "").strip() or None
        if (parent_id is None) != (parent_name is None):
            raise ValueError(f"ByteDance job has an incomplete parent category: {raw.get('id')}")

        source_url = f"https://jobs.bytedance.com/{channel.value}/position/{raw['id']}/detail"
        return JobRecord(
            source_key="bytedance_cn",
            external_id=str(raw["id"]),
            external_code=str(raw["code"]).strip() if raw.get("code") else None,
            source_url=source_url,
            company_name="字节跳动",
            channel=channel,
            employment_type_id=str(recruit_type.get("id") or ""),
            employment_type_name=str(recruit_type.get("name") or "未知"),
            recruitment_project_id=str(subject.get("id")) if subject.get("id") else None,
            recruitment_project_name=str(subject_name) if subject_name else None,
            title=str(raw.get("title") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            requirements=str(raw.get("requirement") or "").strip(),
            published_at=datetime.fromtimestamp(published_ms / 1000, tz=UTC),
            is_hot=ByteDanceConnector._as_bool(raw.get("job_hot_flag")),
            locations=locations,
            categories=[
                SourceCategoryRecord(
                    external_id=category_id,
                    name=category_name,
                    parent_external_id=parent_id,
                    parent_name=parent_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload=raw,
        )

    @staticmethod
    def _parse_locations(
        cities: list[dict[str, Any]], addresses: list[dict[str, Any]]
    ) -> list[LocationRecord]:
        address_by_city = {
            str(item.get("city", {}).get("city_code")): item
            for item in addresses
            if item.get("city", {}).get("city_code")
        }
        if not cities:
            cities = [
                {
                    "code": item.get("city", {}).get("city_code"),
                    "name": item.get("city", {}).get("name"),
                }
                for item in addresses
                if item.get("city", {}).get("city_code")
            ]

        locations: list[LocationRecord] = []
        seen: set[str] = set()
        for city in cities:
            code = str(city.get("code") or "")
            name = str(city.get("name") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            address = address_by_city.get(code) or {}
            state = address.get("state") or {}
            country = address.get("country") or {}
            district = address.get("district") or {}
            locations.append(
                LocationRecord(
                    code=code,
                    name=name,
                    country_code=country.get("country_code"),
                    country_name=country.get("name"),
                    state_code=state.get("state_code"),
                    state_name=state.get("name"),
                    district_code=district.get("district_code"),
                    district_name=district.get("name"),
                    address=address.get("name"),
                )
            )
        return locations

    @staticmethod
    def _as_bool(value: Any) -> bool:
        return value is True or value in (1, "1", "true", "True")
