"""Tencent public campus-recruitment connector."""

import asyncio
import json
from math import ceil
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.browser_json import (
    JsonResponseQueue,
    drain_json_responses,
    enqueue_json_response,
    next_json_payload,
)
from job_market.connectors.retry import retry_async
from job_market.raw_store import RawStore
from job_market.schemas import (
    BusinessUnitRecord,
    CategoryAssignmentMethod,
    Channel,
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

RESPONSE_TIMEOUT_SECONDS = 30
UI_PAGE_SIZE = 10
DETAIL_CONCURRENCY = 2
DETAIL_OPEN_ATTEMPTS = 3
POSITION_LIST_URL = "https://join.qq.com/post.html?query=p_1"
POSITION_DETAIL_URL = "https://join.qq.com/post_detail.html?postid={post_id}"
POSITION_DETAIL_API_URL = (
    "https://join.qq.com/api/v1/jobDetails/getJobDetailsByPostId"
)


class TencentConnector:
    """Collect all public Tencent campus projects and each public detail page."""

    source_key = "tencent_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_started_at = 0.0
        self._request_start_lock = asyncio.Lock()
        self._search_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)
        self.issues: list[CollectionIssue] = []

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.CAMPUS:
            raise ValueError("Tencent connector supports only the campus channel")

        payload = await self._open_all_positions()
        first = self._search_page(payload, expected_page=1)
        total_count = first["count"]
        total_pages = ceil(total_count / UI_PAGE_SIZE) if total_count else 0
        list_rows: list[dict[str, Any]] = []
        seen_post_ids: set[str] = set()
        complete = True

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            current = self._search_page(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_list_payload(channel, page_number, payload)
            for raw in current["rows"]:
                post_id = _optional(raw.get("postId"))
                if post_id is None:
                    complete = False
                    self.issues.append(
                        CollectionIssue(
                            scope="job",
                            partition="all",
                            page=page_number,
                            error_type="MissingJobIdentifier",
                            message="Tencent list row has no postId",
                        )
                    )
                    continue
                if post_id in seen_post_ids:
                    complete = False
                    self.issues.append(
                        CollectionIssue(
                            scope="job",
                            partition="all",
                            page=page_number,
                            external_id=post_id,
                            error_type="RepeatedJob",
                            message="Tencent list repeated a job during pagination",
                        )
                    )
                    continue
                seen_post_ids.add(post_id)
                list_rows.append(raw)
            if page_number < total_pages:
                try:
                    payload = await self._next_search_page(page_number + 1)
                except Exception as exc:
                    complete = False
                    self.issues.append(
                        CollectionIssue(
                            scope="page",
                            partition="all",
                            page=page_number + 1,
                            error_type=type(exc).__name__,
                            message=str(exc) or "Tencent list page remained unavailable",
                        )
                    )
                    break

        if complete and len(list_rows) != total_count:
            complete = False
            self.issues.append(
                CollectionIssue(
                    scope="partition",
                    partition="all",
                    error_type="DynamicListDidNotConverge",
                    message=(
                        "Tencent pagination count mismatch: "
                        f"declared={total_count}, unique={len(list_rows)}"
                    ),
                )
            )

        detail_results = await self._collect_details(list_rows)
        jobs: list[JobRecord] = []
        retired_count = 0
        for index, (post_id, record, detail_payload) in enumerate(detail_results):
            self._save_detail_payload(channel, post_id, index, detail_payload)
            if record is None:
                retired_count += 1
            else:
                jobs.append(record)
        if len(detail_results) != len(list_rows):
            complete = False

        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts={
                "all": total_count,
                "active-detail": len(jobs),
                "explicitly-removed-detail": retired_count,
            },
            pages_fetched=self.pages_fetched,
            complete=complete,
            issues=self.issues,
        )

    async def _open_all_positions(self) -> dict[str, Any]:
        await self._rate_limit()
        self._drain_queues()
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await self._next_payload(self._search_responses, "positions:default-filter")

        await self._rate_limit()
        drain_json_responses(self._search_responses)
        clear_button = self.page.get_by_role("button", name="清除全部", exact=True)
        if await clear_button.count() != 1:
            raise RuntimeError("Tencent page has no unique clear-filters button")
        await clear_button.click()
        return await self._next_payload(self._search_responses, "positions:all:1")

    async def _next_search_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._search_responses)
        next_icon = self.page.locator("i.el-icon.el-icon-arrow-right")
        if await next_icon.count() != 1:
            raise RuntimeError("Tencent pagination has no unique next-page control")
        await next_icon.click()
        payload = await self._next_payload(
            self._search_responses,
            f"positions:all:{page_number}",
        )
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected: int) -> None:
        active = self.page.locator("li.number.active")
        if await active.count() != 1:
            raise RuntimeError("Tencent pagination has no unique active page")
        text = (await active.inner_text()).strip()
        if text != str(expected):
            raise RuntimeError(
                f"Tencent page mismatch: expected={expected}, got={text!r}"
            )

    async def _collect_details(
        self,
        list_rows: list[dict[str, Any]],
    ) -> list[tuple[str, JobRecord | None, dict[str, Any]]]:
        if not list_rows:
            return []

        pending: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        for item in enumerate(list_rows):
            pending.put_nowait(item)
        worker_count = min(DETAIL_CONCURRENCY, len(list_rows))
        for _ in range(worker_count):
            pending.put_nowait(None)

        results: list[tuple[str, JobRecord | None, dict[str, Any]] | None] = [
            None
        ] * len(list_rows)

        async def worker() -> None:
            while True:
                item = await pending.get()
                if item is None:
                    return
                index, list_row = item
                post_id = _optional(list_row.get("postId")) or f"list-index-{index}"
                try:
                    payload = await retry_async(
                        lambda post_id=post_id: self._fetch_detail(post_id),
                        source=self.source_key,
                        operation_name=f"detail:{post_id}",
                        attempts=DETAIL_OPEN_ATTEMPTS,
                    )
                    record = self._parse_detail_response(payload, post_id)
                    list_title = _optional(list_row.get("positionTitle"))
                    if (
                        record is not None
                        and list_title is not None
                        and list_title != record.title
                    ):
                        raise RuntimeError(
                            f"Tencent title changed between list and detail for {post_id}"
                        )
                    results[index] = (post_id, record, payload)
                except Exception as exc:
                    self.issues.append(
                        CollectionIssue(
                            scope="job",
                            partition="detail",
                            external_id=post_id,
                            error_type=type(exc).__name__,
                            message=str(exc) or "Tencent detail collection failed",
                            retry_count=DETAIL_OPEN_ATTEMPTS - 1,
                        )
                    )

        await asyncio.gather(*(worker() for _ in range(worker_count)))

        return [result for result in results if result is not None]

    async def _fetch_detail(self, post_id: str) -> dict[str, Any]:
        await self._rate_limit()
        result = await self.page.evaluate(
            """async ({url, postId}) => {
                const response = await fetch(
                    `${url}?postId=${encodeURIComponent(postId)}`,
                    {credentials: "same-origin"},
                );
                return {httpStatus: response.status, payload: await response.json()};
            }""",
            {"url": POSITION_DETAIL_API_URL, "postId": post_id},
        )
        if not isinstance(result, dict) or result.get("httpStatus") != 200:
            status = result.get("httpStatus") if isinstance(result, dict) else None
            raise RuntimeError(f"Tencent detail {post_id} returned HTTP {status}")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Tencent detail {post_id} returned invalid JSON")
        return payload

    def _parse_detail_response(
        self,
        payload: dict[str, Any],
        post_id: str,
    ) -> JobRecord | None:
        if (
            payload.get("status") == 404
            and payload.get("message") == "岗位已下架"
            and payload.get("data") is None
        ):
            return None
        if payload.get("status") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Tencent detail {post_id}: {message}")
        record = self.parse_job(payload["data"])
        if record.external_id != post_id:
            raise RuntimeError(
                f"Tencent detail response mismatch: requested={post_id}, "
                f"got={record.external_id}"
            )
        return record

    async def _next_payload(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Tencent response: {operation}",
        )
        if payload.get("status") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Tencent response for {operation}: {message}")
        return payload

    @staticmethod
    def _search_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Tencent search data is not an object")
        rows = data.get("positionList")
        count = data.get("count")
        if not isinstance(rows, list):
            raise RuntimeError("Tencent search response has no positionList")
        if not isinstance(count, int) or count < 0:
            raise RuntimeError(f"Tencent returned invalid count: {count!r}")
        if count and not rows:
            raise RuntimeError(f"Tencent returned an empty non-terminal page {expected_page}")
        if len(rows) > UI_PAGE_SIZE:
            raise RuntimeError(
                f"Tencent page {expected_page} exceeds UI page size {UI_PAGE_SIZE}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"Tencent page {expected_page} contains a non-object row")
        return {"rows": rows, "count": count}

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if "/api/v1/position/searchPosition" in response.url:
            enqueue_json_response(self._search_responses, response)

    async def _rate_limit(self) -> None:
        async with self._request_start_lock:
            loop = asyncio.get_running_loop()
            delay = self.settings.tencent_request_delay_seconds - (
                loop.time() - self._last_request_started_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started_at = loop.time()

    def _drain_queues(self) -> None:
        drain_json_responses(self._search_responses)

    def _save_list_payload(
        self,
        channel: Channel,
        page_number: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="all",
                    offset=page_number - 1,
                    payload=payload,
                )
            )

    def _save_detail_payload(
        self,
        channel: Channel,
        post_id: str,
        offset: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition=f"detail-{post_id}",
                    offset=offset,
                    payload=payload,
                )
            )

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _optional(raw.get("postId")) or _required(
            raw,
            "id",
            "Tencent job id",
        )
        title = _required(raw, "title", f"Tencent job title ({external_id})")
        description = _optional(raw.get("desc"))
        requirements = _optional(raw.get("request"))
        employment_type_id = _required(
            raw,
            "recruitType",
            f"Tencent recruit type ({external_id})",
        )
        employment_type_name = _required(
            raw,
            "recruitLabelName",
            f"Tencent recruit label ({external_id})",
        )
        locations = _locations(raw.get("workCityList"), raw.get("workCity"), external_id)
        category_name = _optional(raw.get("tidName"))
        category_id = _optional(raw.get("tid"))
        categories = []
        if category_name is not None:
            categories.append(
                SourceCategoryRecord(
                    external_id=category_id or f"positionFamily:{category_name}",
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            )

        return JobRecord(
            source_key="tencent_cn",
            external_id=external_id,
            external_code=_optional(raw.get("id")),
            source_url=POSITION_DETAIL_URL.format(post_id=external_id),
            company_name="腾讯",
            channel=Channel.CAMPUS,
            employment_type_id=employment_type_id,
            employment_type_name=employment_type_name,
            recruitment_project_id=_optional(raw.get("projectId")),
            recruitment_project_name=_optional(raw.get("projectName")),
            title=title,
            description=description,
            requirements=requirements,
            source_status=_optional(raw.get("status")),
            interview_location_names=_unique_strings(raw.get("recruitCityList")),
            locations=locations,
            categories=categories,
            business_units=_business_units(raw.get("intentionBGDList"), external_id),
            source_payload=raw,
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


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(text for item in value if (text := _optional(item))))


def _locations(names_value: Any, codes_value: Any, external_id: str) -> list[LocationRecord]:
    if not isinstance(names_value, list):
        return []
    codes = []
    if isinstance(codes_value, str):
        codes = [part.strip() for part in codes_value.split(",") if part.strip()]
    elif isinstance(codes_value, list):
        codes = [_optional(part) for part in codes_value]

    locations: list[LocationRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(names_value):
        name = _optional(item)
        if name is None:
            continue
        code = _optional(codes[index]) if index < len(codes) else None
        code = code or f"city:{name}"
        if code in seen:
            continue
        seen.add(code)
        locations.append(LocationRecord(code=code, name=name))
    return locations


def _business_units(value: Any, external_id: str) -> list[BusinessUnitRecord]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Tencent intentionBGDList is not a list: {external_id}")
    units: list[BusinessUnitRecord] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Tencent business group is not an object: {external_id}")
        code = _optional(item.get("id"))
        name = (
            _optional(item.get("title"))
            or _optional(item.get("showTxt"))
            or _optional(item.get("showTitle"))
        )
        if code is None or name is None:
            raise ValueError(f"Tencent business group has no id/name: {external_id}")
        if code in seen:
            continue
        seen.add(code)
        units.append(BusinessUnitRecord(code=code, name=name))
    return units
