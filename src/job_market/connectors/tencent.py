"""Tencent public campus-recruitment connector."""

import asyncio
import json
from math import ceil
from typing import Any

from playwright.async_api import Page, Response, Route

from job_market.config import Settings
from job_market.connectors.browser_json import (
    BrowserResponseUnavailableError,
    JsonResponseQueue,
    drain_json_responses,
    enqueue_json_response,
    next_json_payload,
)
from job_market.raw_store import RawStore
from job_market.schemas import (
    BusinessUnitRecord,
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
DETAIL_CONCURRENCY = 2
DETAIL_OPEN_ATTEMPTS = 3
POSITION_LIST_URL = "https://join.qq.com/post.html?query=p_1"
POSITION_DETAIL_URL = "https://join.qq.com/post_detail.html?postid={post_id}"


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
        self._detail_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.CAMPUS:
            raise ValueError("Tencent connector supports only the campus channel")

        await self.page.route("**/*", _skip_nonessential_assets)
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
                post_id = _required(raw, "postId", "Tencent list post id")
                if post_id in seen_post_ids:
                    raise RuntimeError(f"Tencent list repeated post id {post_id}")
                seen_post_ids.add(post_id)
                list_rows.append(raw)
            if page_number < total_pages:
                payload = await self._next_search_page(page_number + 1)

        if complete and len(list_rows) != total_count:
            raise RuntimeError(
                "Tencent pagination count mismatch: "
                f"declared={total_count}, unique={len(list_rows)}"
            )

        detail_results = await self._collect_details(list_rows)
        jobs: list[JobRecord] = []
        for index, (record, detail_payload) in enumerate(detail_results):
            post_id = record.external_id
            self._save_detail_payload(channel, post_id, index, detail_payload)
            jobs.append(record)

        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts={"all": total_count},
            pages_fetched=self.pages_fetched,
            complete=complete,
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
    ) -> list[tuple[JobRecord, dict[str, Any]]]:
        if not list_rows:
            return []

        workers: list[tuple[Page, JsonResponseQueue]] = [
            (self.page, self._detail_responses)
        ]
        extra_pages: list[Page] = []
        for _ in range(min(DETAIL_CONCURRENCY, len(list_rows)) - 1):
            detail_page = await self.page.context.new_page()
            await detail_page.route("**/*", _skip_nonessential_assets)
            detail_queue: JsonResponseQueue = asyncio.Queue()
            detail_page.on(
                "response",
                lambda response, queue=detail_queue: self._record_detail_response(
                    response, queue
                ),
            )
            workers.append((detail_page, detail_queue))
            extra_pages.append(detail_page)

        pending: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        for item in enumerate(list_rows):
            pending.put_nowait(item)
        for _ in workers:
            pending.put_nowait(None)

        results: list[tuple[JobRecord, dict[str, Any]] | None] = [None] * len(
            list_rows
        )

        async def worker(page: Page, queue: JsonResponseQueue) -> None:
            while True:
                item = await pending.get()
                if item is None:
                    return
                index, list_row = item
                post_id = _required(list_row, "postId", "Tencent list post id")
                payload = await self._open_detail(page, queue, post_id)
                record = self.parse_job(payload["data"])
                if record.external_id != post_id:
                    raise RuntimeError(
                        "Tencent detail mismatch: "
                        f"requested={post_id}, got={record.external_id}"
                    )
                list_title = _optional(list_row.get("positionTitle"))
                if list_title is not None and list_title != record.title:
                    raise RuntimeError(
                        f"Tencent title changed between list and detail for {post_id}"
                    )
                results[index] = (record, payload)

        try:
            await asyncio.gather(*(worker(page, queue) for page, queue in workers))
        finally:
            await asyncio.gather(
                *(page.close() for page in extra_pages),
                return_exceptions=True,
            )

        if any(result is None for result in results):
            raise RuntimeError("Tencent detail collection finished with missing results")
        return [result for result in results if result is not None]

    async def _open_detail(
        self,
        page: Page,
        queue: JsonResponseQueue,
        post_id: str,
    ) -> dict[str, Any]:
        for attempt in range(1, DETAIL_OPEN_ATTEMPTS + 1):
            try:
                return await self._open_detail_once(page, queue, post_id)
            except BrowserResponseUnavailableError as exc:
                if attempt == DETAIL_OPEN_ATTEMPTS:
                    raise BrowserResponseUnavailableError(
                        f"Tencent detail {post_id} did not load after "
                        f"{DETAIL_OPEN_ATTEMPTS} attempts"
                    ) from exc
        raise AssertionError("unreachable")

    async def _open_detail_once(
        self,
        page: Page,
        queue: JsonResponseQueue,
        post_id: str,
    ) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(queue)
        await page.goto(
            POSITION_DETAIL_URL.format(post_id=post_id),
            # The source fact arrives through the page's JSON request. Waiting
            # for the full detail DOM adds several seconds of unrelated asset
            # work per position and does not improve response integrity.
            wait_until="commit",
            timeout=60_000,
        )
        payload = await self._next_payload(
            queue,
            f"detail:{post_id}",
        )
        actual = _optional(payload["data"].get("postId"))
        if actual != post_id:
            raise RuntimeError(
                f"Tencent detail response mismatch: requested={post_id}, got={actual!r}"
            )
        return payload

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
        elif "/api/v1/jobDetails/getJobDetailsByPostId" in response.url:
            enqueue_json_response(self._detail_responses, response)

    @staticmethod
    def _record_detail_response(
        response: Response,
        queue: JsonResponseQueue,
    ) -> None:
        if (
            response.status == 200
            and "/api/v1/jobDetails/getJobDetailsByPostId" in response.url
        ):
            enqueue_json_response(queue, response)

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
        drain_json_responses(self._detail_responses)

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
        external_id = _required(raw, "postId", "Tencent job id")
        title = _required(raw, "title", f"Tencent job title ({external_id})")
        description = _required(raw, "desc", f"Tencent job description ({external_id})")
        requirements = _required(raw, "request", f"Tencent job requirement ({external_id})")
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


async def _skip_nonessential_assets(route: Route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


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
        raise ValueError(f"Tencent job has no workCityList: {external_id}")
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
    if not locations:
        raise ValueError(f"Tencent job has no work location: {external_id}")
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
