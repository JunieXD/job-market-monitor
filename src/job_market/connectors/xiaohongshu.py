"""Xiaohongshu public experienced-recruitment connector."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

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

RESPONSE_TIMEOUT_SECONDS = 30
UI_PAGE_SIZE = 10
MAX_PARTITION_ATTEMPTS = 5
POSITION_LIST_URL = "https://job.xiaohongshu.com/social/position"
POSITION_DETAIL_URL = "https://job.xiaohongshu.com/social/position/{external_id}"
POSITION_ENDPOINT = "/websiterecruit/position/pageQueryPosition"
CATEGORY_GROUP_SELECTOR = ".ant-checkbox-group.flex.flex-col.gap-y-2"


@dataclass(frozen=True)
class CategoryPartition:
    code: str
    name: str

    @property
    def label(self) -> str:
        return f"category-{self.code}"


class XiaohongshuConnector:
    """Collect social jobs through official category filters and pagination."""

    source_key = "xiaohongshu_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self._active_category: str | None = None
        self._root_observations: list[int] = []
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Xiaohongshu connector supports only experienced jobs")

        root_payload = await self._observe_root(channel)
        root_total = self._position_page(root_payload, expected_page=1)["total"]
        categories = await self._category_partitions()
        jobs_by_id: dict[str, JobRecord] = {}
        partition_counts: dict[str, int] = {"all": root_total}
        complete = True

        for index, category in enumerate(categories):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            if index:
                await self._observe_root(channel)

            first_payload = await self._select_category(category)
            category_jobs, category_total, category_complete = (
                await self._collect_category(
                    channel,
                    category,
                    first_payload,
                    max_pages,
                )
            )
            partition_counts[category.label] = category_total
            if not category_complete:
                complete = False

            for external_id, record in category_jobs.items():
                previous = jobs_by_id.get(external_id)
                if previous is not None:
                    if previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Xiaohongshu job {external_id} conflicts across categories"
                        )
                    raise RuntimeError(
                        f"Xiaohongshu job {external_id} appears in multiple categories"
                    )
                jobs_by_id[external_id] = record
            if not complete:
                break

        if complete:
            category_sum = sum(
                count
                for label, count in partition_counts.items()
                if label.startswith("category-")
            )
            if len(jobs_by_id) != category_sum:
                raise RuntimeError(
                    "Xiaohongshu category coverage mismatch: "
                    f"categories={category_sum}, unique={len(jobs_by_id)}"
                )

        partition_counts["collected-unique"] = len(jobs_by_id)
        partition_counts.update(
            {
                f"root-observation-{index:02d}": total
                for index, total in enumerate(self._root_observations, start=1)
            }
        )
        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_category(
        self,
        channel: Channel,
        category: CategoryPartition,
        initial_payload: dict[str, Any],
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool]:
        union_by_id: dict[str, JobRecord] = {}
        target_total: int | None = None

        for attempt in range(1, MAX_PARTITION_ATTEMPTS + 1):
            if attempt == 1:
                payload = initial_payload
            else:
                await self._observe_root(channel)
                payload = await self._select_category(category)

            first = self._position_page(payload, expected_page=1)
            current_total = first["total"]
            if target_total is None:
                target_total = current_total
            elif current_total != target_total:
                target_total = current_total
                union_by_id = {}

            pass_by_id: dict[str, JobRecord] = {}
            total_pages = ceil(target_total / UI_PAGE_SIZE) if target_total else 0
            attempt_label = (
                category.label
                if attempt == 1
                else f"{category.label}-retry-{attempt}"
            )
            if total_pages == 0:
                self._save_payload(channel, attempt_label, 0, payload)

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, target_total, False
                await self._assert_active_page(page_number)
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != target_total:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    attempt_label,
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Xiaohongshu job {record.external_id} changed within "
                            f"{attempt_label}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._next_page(page_number + 1)
            else:
                for external_id, record in pass_by_id.items():
                    previous = union_by_id.get(external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Xiaohongshu job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        f"Xiaohongshu {category.label} exceeded its declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            f"Xiaohongshu {category.label} did not converge after "
            f"{MAX_PARTITION_ATTEMPTS} attempts: declared={target_total}, "
            f"union={len(union_by_id)}"
        )

    async def _observe_root(self, channel: Channel) -> dict[str, Any]:
        payload = await self._open_root()
        root = self._position_page(payload, expected_page=1)
        self._root_observations.append(root["total"])
        self._save_payload(
            channel,
            f"root-observation-{len(self._root_observations):02d}",
            0,
            payload,
        )
        return payload

    async def _open_root(self) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = 1
        self._active_category = None
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_payload("positions:root:1")
        await self._assert_active_page(1)
        return payload

    async def _category_partitions(self) -> tuple[CategoryPartition, ...]:
        groups = self.page.locator(CATEGORY_GROUP_SELECTOR)
        if await groups.count() != 1:
            raise RuntimeError("Xiaohongshu category filter group is not unique")
        labels = groups.locator("label")
        categories: list[CategoryPartition] = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            input_element = label.locator('input[type="checkbox"]')
            code = (await input_element.get_attribute("value") or "").strip()
            name = (await label.inner_text()).strip()
            if not code or not name:
                raise RuntimeError("Xiaohongshu category filter is incomplete")
            categories.append(CategoryPartition(code=code, name=name))
        if not categories or len({item.code for item in categories}) != len(categories):
            raise RuntimeError("Xiaohongshu category catalog is empty or duplicated")
        return tuple(categories)

    async def _select_category(
        self,
        category: CategoryPartition,
    ) -> dict[str, Any]:
        option = self.page.locator(
            f'{CATEGORY_GROUP_SELECTOR} input[type="checkbox"][value="{category.code}"]'
        )
        if await option.count() != 1:
            raise RuntimeError(
                f"Xiaohongshu category option {category.code!r} is not unique"
            )
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = 1
        self._active_category = category.code
        await option.click()
        payload = await self._next_payload(f"positions:{category.label}:1")
        await self._assert_active_page(1)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_button = self.page.locator(".ant-pagination-next button")
        if await next_button.count() != 1 or await next_button.is_disabled():
            raise RuntimeError(
                f"Xiaohongshu pagination ended before page {page_number}"
            )
        await next_button.click()
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".ant-pagination-item-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"Xiaohongshu page did not render active page {expected_page}"
        )

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Xiaohongshu response: {operation}",
        )
        if payload.get("statusCode") != 200 or not isinstance(
            payload.get("data"), dict
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(
                f"Invalid Xiaohongshu response for {operation}: {message}"
            )
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Xiaohongshu response has no data object")
        rows = data.get("list")
        total = _response_int(data.get("total"), "total", allow_zero=True)
        page_number = _response_int(data.get("pageNum"), "pageNum")
        page_size = _response_int(data.get("pageSize"), "pageSize")
        total_pages = _response_int(
            data.get("totalPage"),
            "totalPage",
            allow_zero=True,
        )
        if page_number != expected_page:
            raise RuntimeError(
                f"Xiaohongshu page mismatch: expected={expected_page}, "
                f"got={page_number}"
            )
        if page_size != UI_PAGE_SIZE:
            raise RuntimeError(
                f"Xiaohongshu page size changed: expected={UI_PAGE_SIZE}, "
                f"got={page_size}"
            )
        expected_pages = ceil(total / page_size) if total else 0
        if total_pages != expected_pages:
            raise RuntimeError(
                "Xiaohongshu page count mismatch: "
                f"expected={expected_pages}, got={total_pages}"
            )
        if not isinstance(rows, list):
            raise RuntimeError("Xiaohongshu response has no job list")
        expected_rows = min(
            page_size,
            max(total - (expected_page - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Xiaohongshu page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Xiaohongshu job list contains a non-object row")
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "positionId", "Xiaohongshu job id")
        title = _required(
            raw,
            "positionName",
            f"Xiaohongshu job title ({external_id})",
        )
        location_codes = _required(
            raw,
            "workplaceIds",
            f"Xiaohongshu job location codes ({external_id})",
        ).split(",")
        location_names = re.split(",|，", _required(
            raw,
            "workplace",
            f"Xiaohongshu job location names ({external_id})",
        ))
        location_codes = [item.strip() for item in location_codes if item.strip()]
        location_names = [item.strip() for item in location_names if item.strip()]
        if not location_codes or len(location_codes) != len(location_names):
            raise ValueError(
                f"Xiaohongshu job {external_id} has inconsistent locations"
            )
        job_type = _required(
            raw,
            "jobType",
            f"Xiaohongshu job category ({external_id})",
        )

        return JobRecord(
            source_key="xiaohongshu_social_cn",
            external_id=external_id,
            source_url=POSITION_DETAIL_URL.format(external_id=external_id),
            company_name="小红书",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=title,
            description=_required(
                raw,
                "duty",
                f"Xiaohongshu job duty ({external_id})",
            ),
            requirements=_required(
                raw,
                "qualification",
                f"Xiaohongshu job qualification ({external_id})",
            ),
            published_at=_source_date(raw.get("publishTime"), "publishTime"),
            source_status=_optional(raw.get("recruitStatus")),
            recruitment_count=_optional_count(raw.get("amountInNeed"), external_id),
            locations=[
                LocationRecord(code=code, name=name)
                for code, name in zip(location_codes, location_names, strict=True)
            ],
            categories=[
                SourceCategoryRecord(
                    external_id=f"label:{job_type}",
                    name=job_type,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200 or POSITION_ENDPOINT not in response.url:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        expected: dict[str, Any] = {
            "recruitType": "social",
            "positionName": "",
            "pageNum": self._active_page,
            "pageSize": UI_PAGE_SIZE,
        }
        if self._active_category is not None:
            expected["jobTypes"] = [self._active_category]
        if post_data == expected:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.xiaohongshu_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _save_payload(
        self,
        channel: Channel,
        partition: str,
        offset: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition=partition,
                    offset=offset,
                    payload=payload,
                )
            )



def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(raw: dict[str, Any], key: str, label: str) -> str:
    value = _optional(raw.get(key))
    if value is None:
        raise ValueError(f"{label} is missing")
    return value


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Xiaohongshu {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Xiaohongshu {field} is invalid: {value!r}")
    return value


def _optional_count(value: Any, external_id: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"Xiaohongshu job {external_id} amountInNeed is invalid: {value!r}"
        )
    return value


def _source_date(value: Any, field: str) -> datetime | None:
    text_value = _optional(value)
    if text_value is None:
        return None
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(
            f"Xiaohongshu {field} is not an ISO date: {text_value!r}"
        ) from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))
