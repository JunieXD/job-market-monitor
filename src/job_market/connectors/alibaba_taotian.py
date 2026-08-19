"""TaoTian Group public experienced-hire connector."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.alibaba_common import (
    canonical_position_url,
    coded_label,
    number_range,
    time_range,
    timestamp_ms,
    unique_strings,
)
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
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

API_CAP = 10_000
UI_PAGE_SIZE = 10
RESPONSE_TIMEOUT_SECONDS = 30
POSITION_PAGE_URL = "https://talent.taotian.com/off-campus/position-list?lang=zh"
POSITION_URL = "https://talent.taotian.com/off-campus/position-detail?positionId={external_id}"


@dataclass(frozen=True)
class TaoTianCategory:
    code: str
    name: str

    @property
    def partition(self) -> str:
        return f"category-{self.code}-{self.name}"

    def assignment(self) -> SourceCategoryRecord:
        return SourceCategoryRecord(
            external_id=self.code,
            name=self.name,
            assignment_method=CategoryAssignmentMethod.FILTER_MEMBERSHIP,
        )


class AlibabaTaoTianConnector:
    """Collect TaoTian jobs and their official top-level filter memberships."""

    source_key = "alibaba_taotian_social"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self.issues: list[CollectionIssue] = []
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("TaoTian connector supports only the experienced channel")

        category_payload, payload = await self._open_portal()
        categories = self._parse_categories(category_payload)
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="category-catalog",
                    offset=0,
                    payload=category_payload,
                )
            )

        total = self._position_total(payload, "all")
        if total >= API_CAP:
            raise RuntimeError(
                f"TaoTian root result reached the {API_CAP} API cap; "
                "add another source-specific partition before collecting it."
            )
        partition_counts = {"all": total}
        jobs_by_id: dict[str, JobRecord] = {}
        complete, payload = await self._collect_root_pages(
            channel,
            payload,
            total,
            jobs_by_id,
            max_pages,
        )
        del payload
        memberships: dict[str, list[SourceCategoryRecord]] = {
            external_id: [] for external_id in jobs_by_id
        }
        if complete:
            complete = await self._collect_category_memberships(
                channel,
                categories,
                jobs_by_id,
                memberships,
                partition_counts,
                max_pages,
            )

        jobs = [
            record.model_copy(update={"categories": memberships[external_id]})
            for external_id, record in jobs_by_id.items()
        ]
        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
            issues=self.issues,
        )

    async def _open_portal(self) -> tuple[dict[str, Any], dict[str, Any]]:
        await self._rate_limit()
        self._drain_queues()
        await self.page.goto(
            POSITION_PAGE_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        category_payload = await self._next_payload(
            self._category_responses,
            "category catalog",
        )
        payload = await self._next_payload(self._position_responses, "positions:all:1")
        return category_payload, payload

    async def _collect_root_pages(
        self,
        channel: Channel,
        payload: dict[str, Any],
        total: int,
        jobs_by_id: dict[str, JobRecord],
        max_pages: int | None,
    ) -> tuple[bool, dict[str, Any]]:
        offset = 0
        while offset < total:
            if max_pages is not None and self.pages_fetched >= max_pages:
                return False, payload
            self.pages_fetched += 1
            self._save_payload(channel, "all", offset, payload)
            rows = self._position_rows(payload, "all", offset)
            if not rows:
                raise RuntimeError(f"TaoTian returned an empty root page at offset {offset}")
            for raw in rows:
                try:
                    record = self.parse_job(raw)
                except (TypeError, ValueError) as exc:
                    self.issues.append(
                        CollectionIssue(
                            scope="job",
                            partition="all",
                            page=offset // UI_PAGE_SIZE + 1,
                            external_id=str(raw.get("id") or "").strip() or None,
                            error_type=type(exc).__name__,
                            message=str(exc) or "TaoTian job parsing failed",
                        )
                    )
                    continue
                previous = jobs_by_id.get(record.external_id)
                if previous is None:
                    jobs_by_id[record.external_id] = record
                elif previous.content_hash() != record.content_hash():
                    self.issues.append(
                        CollectionIssue(
                            scope="job",
                            partition="all",
                            external_id=record.external_id,
                            error_type="JobChangedDuringPagination",
                            message="TaoTian job changed while the root list was paginated",
                        )
                    )
            offset += len(rows)
            if offset < total:
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return False, payload
                try:
                    payload = await self._next_page("all", offset)
                except Exception as exc:
                    self.issues.append(
                        CollectionIssue(
                            scope="page",
                            partition="all",
                            page=offset // UI_PAGE_SIZE + 1,
                            error_type=type(exc).__name__,
                            message=str(exc) or "TaoTian list page remained unavailable",
                        )
                    )
                    return False, payload
        complete = len(jobs_by_id) == total and not self.issues
        if not complete:
            self.issues.append(
                CollectionIssue(
                    scope="partition",
                    partition="all",
                    error_type="DynamicListDidNotConverge",
                    message=(
                        "TaoTian root count mismatch: "
                        f"declared={total}, unique={len(jobs_by_id)}"
                    ),
                )
            )
        return complete, payload

    async def _collect_category_memberships(
        self,
        channel: Channel,
        categories: list[TaoTianCategory],
        jobs_by_id: dict[str, JobRecord],
        memberships: dict[str, list[SourceCategoryRecord]],
        partition_counts: dict[str, int],
        max_pages: int | None,
    ) -> bool:
        previous: TaoTianCategory | None = None
        all_categories_complete = True
        for category in categories:
            if max_pages is not None and self.pages_fetched >= max_pages:
                return False
            if previous is not None:
                await self._set_category(previous, selected=False)
            payload = await self._set_category(category, selected=True)
            previous = category
            total = self._position_total(payload, category.partition)
            if total >= API_CAP:
                raise RuntimeError(
                    f"TaoTian category {category.name!r} reached the {API_CAP} API cap"
                )
            partition_counts[category.partition] = total
            offset = 0
            seen_ids: set[str] = set()
            category_problem_types: set[str] = set()
            while offset < total:
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return False
                self.pages_fetched += 1
                self._save_payload(channel, category.partition, offset, payload)
                rows = self._position_rows(payload, category.partition, offset)
                if not rows:
                    raise RuntimeError(
                        "TaoTian returned an empty category page: "
                        f"{category.name} offset={offset}"
                    )
                for raw in rows:
                    try:
                        candidate = self.parse_job(raw)
                    except (TypeError, ValueError):
                        category_problem_types.add("job_parse_error")
                        continue
                    stored = jobs_by_id.get(candidate.external_id)
                    if stored is None:
                        category_problem_types.add("job_absent_from_root")
                        continue
                    if candidate.content_hash() != stored.content_hash():
                        category_problem_types.add("job_changed_during_membership_walk")
                    if candidate.external_id in seen_ids:
                        category_problem_types.add("repeated_job")
                        continue
                    seen_ids.add(candidate.external_id)
                    memberships[candidate.external_id].append(category.assignment())
                offset += len(rows)
                if offset < total:
                    if max_pages is not None and self.pages_fetched >= max_pages:
                        return False
                    try:
                        payload = await self._next_page(category.partition, offset)
                    except Exception as exc:
                        category_problem_types.add("page_unavailable")
                        self.issues.append(
                            CollectionIssue(
                                scope="page",
                                partition=category.partition,
                                page=offset // UI_PAGE_SIZE + 1,
                                error_type=type(exc).__name__,
                                message=(
                                    str(exc)
                                    or "TaoTian category page remained unavailable"
                                ),
                            )
                        )
                        break
            if len(seen_ids) != total:
                category_problem_types.add("count_mismatch")
            if category_problem_types:
                all_categories_complete = False
                self.issues.append(
                    CollectionIssue(
                        scope="partition",
                        partition=category.partition,
                        error_type="CategoryMembershipIncomplete",
                        message=(
                            f"TaoTian category {category.name!r} was incomplete: "
                            f"declared={total}, unique={len(seen_ids)}, "
                            f"problems={','.join(sorted(category_problem_types))}"
                        ),
                    )
                )
        return all_categories_complete

    async def _set_category(
        self,
        category: TaoTianCategory,
        *,
        selected: bool,
    ) -> dict[str, Any]:
        await self._rate_limit()
        self._drain_queue(self._position_responses)
        selector = f"input[aria-label={json.dumps(category.name, ensure_ascii=False)}]"
        checkbox = self.page.locator(selector)
        if await checkbox.count() != 1:
            raise RuntimeError(
                f"Cannot uniquely locate TaoTian category filter: {category.name!r}"
            )
        if await checkbox.is_checked() == selected:
            raise RuntimeError(
                f"TaoTian category filter has an unexpected state: {category.name!r}"
            )
        await checkbox.click()
        state = "selected" if selected else "cleared"
        return await self._next_payload(
            self._position_responses,
            f"positions:{category.code}:{state}",
        )

    async def _next_page(self, partition: str, offset: int) -> dict[str, Any]:
        await self._rate_limit()
        self._drain_queue(self._position_responses)
        next_button = self.page.locator("button[aria-label^='下一页']")
        if await next_button.count() != 1:
            raise RuntimeError("Cannot uniquely locate TaoTian next-page button")
        if await next_button.get_attribute("disabled") is not None:
            raise RuntimeError(
                f"TaoTian pagination ended early: {partition} offset={offset}"
            )
        await next_button.click()
        return await self._next_payload(
            self._position_responses,
            f"positions:{partition}:{offset // UI_PAGE_SIZE + 1}",
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if "/category/list" in response.url:
            enqueue_json_response(self._category_responses, response)
        elif "/position/search" in response.url:
            enqueue_json_response(self._position_responses, response)

    async def _next_payload(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"TaoTian response: {operation}",
        )
        if payload.get("success") is not True or "content" not in payload:
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid TaoTian response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _rate_limit(self) -> None:
        delay = self.settings.alibaba_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _drain_queues(self) -> None:
        self._drain_queue(self._category_responses)
        self._drain_queue(self._position_responses)

    @staticmethod
    def _drain_queue(queue: JsonResponseQueue) -> None:
        drain_json_responses(queue)

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

    @staticmethod
    def _parse_categories(payload: dict[str, Any]) -> list[TaoTianCategory]:
        content = payload.get("content")
        if not isinstance(content, list):
            raise RuntimeError("TaoTian category response has no list content")
        categories: list[TaoTianCategory] = []
        seen_codes: set[str] = set()
        for item in content:
            if not isinstance(item, dict):
                raise RuntimeError("TaoTian category item is not an object")
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not code or not name:
                raise RuntimeError("TaoTian top-level category has no code or name")
            if code in seen_codes:
                raise RuntimeError(f"TaoTian category code is duplicated: {code!r}")
            seen_codes.add(code)
            categories.append(TaoTianCategory(code=code, name=name))
        if not categories:
            raise RuntimeError("TaoTian exposed no top-level categories")
        return categories

    @staticmethod
    def _position_content(payload: dict[str, Any], partition: str) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise RuntimeError(f"TaoTian position response has no content: {partition}")
        return content

    @classmethod
    def _position_total(cls, payload: dict[str, Any], partition: str) -> int:
        total = cls._position_content(payload, partition).get("totalCount")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError(f"TaoTian returned an invalid totalCount: {partition}")
        return total

    @classmethod
    def _position_rows(
        cls,
        payload: dict[str, Any],
        partition: str,
        offset: int,
    ) -> list[dict[str, Any]]:
        content = cls._position_content(payload, partition)
        current_page = content.get("currentPage")
        expected_page = offset // UI_PAGE_SIZE + 1
        if current_page is not None and current_page != expected_page:
            raise RuntimeError(
                f"TaoTian page mismatch for {partition}: "
                f"expected {expected_page}, got {current_page}"
            )
        rows = content.get("datas")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError(f"TaoTian position response has invalid datas: {partition}")
        return rows

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = str(raw.get("id") or "").strip()
        title = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip() or None
        requirements = str(raw.get("requirement") or "").strip() or None
        if not external_id or not title:
            raise ValueError(f"TaoTian job is missing a required fact: {raw.get('id')!r}")

        locations = [
            LocationRecord(code=f"city:{name}", name=name)
            for name in unique_strings(raw.get("workLocations"), "workLocations")
        ]
        degree_code, degree_name = coded_label(raw.get("degree"), "degree")
        department_code, department_name = coded_label(
            raw.get("department"),
            "department",
            string_is_name=True,
        )
        experience_min, experience_max = number_range(
            raw.get("experience"),
            "experience",
        )
        graduation_start_at, graduation_end_at = time_range(
            raw.get("graduationTime"),
            "graduationTime",
        )
        return JobRecord(
            source_key="alibaba_taotian_social",
            external_id=external_id,
            external_code=str(raw["code"]).strip() if raw.get("code") else None,
            source_url=canonical_position_url(
                raw.get("positionUrl"),
                POSITION_URL.format(external_id=external_id),
                POSITION_PAGE_URL,
            ),
            company_name="阿里巴巴集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=title,
            description=description,
            requirements=requirements,
            published_at=timestamp_ms(raw.get("publishTime"), "publishTime"),
            source_updated_at=timestamp_ms(raw.get("modifyTime"), "modifyTime"),
            source_status=(
                str(raw["status"]).strip() if raw.get("status") is not None else None
            ),
            degree_code=degree_code,
            degree_name=degree_name,
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            graduation_start_at=graduation_start_at,
            graduation_end_at=graduation_end_at,
            department_code=department_code,
            department_name=department_name,
            interview_location_names=unique_strings(
                raw.get("interviewLocations"),
                "interviewLocations",
            ),
            is_hot=None,
            locations=locations,
            categories=[],
            business_units=[],
            source_payload=raw,
        )
