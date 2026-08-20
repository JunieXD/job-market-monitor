"""360 public experienced-recruitment connector."""

import asyncio
import json
import re
from datetime import date, datetime, time
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page, Response

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
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

RESPONSE_TIMEOUT_SECONDS = 30
LIST_LIMIT = 10_000
DETAIL_CONCURRENCY = 2
DETAIL_OPEN_ATTEMPTS = 3
POSITION_LIST_URL = "https://hr.360.cn/hr/list"
POSITION_DETAIL_URL = "https://hr.360.cn/hr/detail/{external_id}"
LIST_ENDPOINT = "/v2/index/getlistsearch"
DETAIL_ENDPOINT = "/v2/index/getjobone"
REQUIREMENT_HEADINGS = {
    "岗位要求",
    "任职要求",
    "二、基本要求",
}


class Qihu360Connector:
    """Collect 360's complete client-side list and each public detail page."""

    source_key = "qihu360_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_started_at = 0.0
        self._request_start_lock = asyncio.Lock()
        self._list_responses: JsonResponseQueue = asyncio.Queue()
        self._detail_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("360 connector supports only the experienced channel")

        payload = await self._open_list()
        rows, total_count = self._list_rows(payload)
        self.pages_fetched = 1
        self._save_payload(channel, "list", 0, payload)

        seen_ids: set[str] = set()
        for row in rows:
            external_id = _required(row, "id", "360 list job id")
            if external_id in seen_ids:
                raise RuntimeError(f"360 list repeated job id {external_id}")
            seen_ids.add(external_id)

        detail_results = await self._collect_details(rows)
        jobs: list[JobRecord] = []
        for index, (record, detail_payload) in enumerate(detail_results):
            self._save_payload(
                channel,
                f"detail-{record.external_id}",
                index,
                detail_payload,
            )
            jobs.append(record)

        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts={"all": total_count},
            pages_fetched=self.pages_fetched,
            complete=True,
        )

    async def _open_list(self) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._list_responses)
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        return await self._next_success(self._list_responses, "positions")

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
            detail_queue: JsonResponseQueue = asyncio.Queue()
            detail_page.on(
                "response",
                lambda response, queue=detail_queue: self._record_detail_response(
                    response,
                    queue,
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
                external_id = _required(list_row, "id", "360 list job id")
                detail_payload = await self._open_detail(page, queue, external_id)
                record = self.parse_job(list_row, detail_payload["data"])
                results[index] = (record, detail_payload)

        try:
            await asyncio.gather(*(worker(page, queue) for page, queue in workers))
        finally:
            await asyncio.gather(
                *(page.close() for page in extra_pages),
                return_exceptions=True,
            )

        if any(result is None for result in results):
            raise RuntimeError("360 detail collection finished with missing results")
        return [result for result in results if result is not None]

    async def _open_detail(
        self,
        page: Page,
        queue: JsonResponseQueue,
        external_id: str,
    ) -> dict[str, Any]:
        for attempt in range(1, DETAIL_OPEN_ATTEMPTS + 1):
            try:
                await self._rate_limit()
                drain_json_responses(queue)
                await page.goto(
                    POSITION_DETAIL_URL.format(external_id=external_id),
                    wait_until="commit",
                    timeout=60_000,
                )
                payload = await self._next_success(queue, f"detail:{external_id}")
                detail = payload.get("data")
                if not isinstance(detail, dict):
                    raise RuntimeError(
                        f"360 detail response {external_id} has no data object"
                    )
                actual = _optional(detail.get("id"))
                if actual != external_id:
                    raise RuntimeError(
                        "360 detail response mismatch: "
                        f"requested={external_id}, got={actual!r}"
                    )
                return payload
            except BrowserResponseUnavailableError as exc:
                if attempt == DETAIL_OPEN_ATTEMPTS:
                    raise BrowserResponseUnavailableError(
                        f"360 detail {external_id} did not load after "
                        f"{DETAIL_OPEN_ATTEMPTS} attempts"
                    ) from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _list_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        rows = payload.get("data")
        total = _response_int(payload.get("count"), "count", allow_zero=True)
        if not isinstance(rows, list):
            raise RuntimeError("360 list response has no data array")
        if total != len(rows):
            raise RuntimeError(
                f"360 list count mismatch: declared={total}, rows={len(rows)}"
            )
        if total >= LIST_LIMIT:
            raise RuntimeError(
                f"360 list reached the {LIST_LIMIT}-row request cap; refusing partial data"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("360 list response contains a non-object row")
        return rows, total

    @staticmethod
    def parse_job(
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any],
    ) -> JobRecord:
        external_id = _required(list_raw, "id", "360 list job id")
        detail_id = _required(detail_raw, "id", "360 detail job id")
        if detail_id != external_id:
            raise ValueError(
                f"360 job identity changed between list and detail: "
                f"{external_id!r} != {detail_id!r}"
            )
        for field in ("title", "type", "position", "area"):
            if list_raw.get(field) != detail_raw.get(field):
                raise ValueError(
                    f"360 job {external_id} changed field {field!r} "
                    "between list and detail"
                )

        title = _required(detail_raw, "title", f"360 job title ({external_id})")
        category = _required(
            detail_raw,
            "type",
            f"360 job category ({external_id})",
        )
        location = _required(
            detail_raw,
            "area",
            f"360 job location ({external_id})",
        )
        description, requirements = _job_sections(detail_raw, external_id)
        experience_min, experience_max = _experience_range(
            detail_raw.get("year"),
            external_id,
        )
        _source_date(list_raw.get("date"), "list.date")

        return JobRecord(
            source_key="qihu360_cn",
            external_id=external_id,
            source_url=POSITION_DETAIL_URL.format(external_id=external_id),
            company_name="360集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_date(detail_raw.get("date"), "detail.date"),
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            locations=[LocationRecord(code=f"name:{location}", name=location)],
            categories=[
                SourceCategoryRecord(
                    external_id=f"label:{category}",
                    name=category,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload={"list": list_raw, "detail": detail_raw},
        )

    async def _next_success(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"360 response: {operation}",
        )
        if payload.get("code") != 0:
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid 360 response for {operation}: {message}")
        return payload

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if LIST_ENDPOINT in response.url:
            try:
                post_data = response.request.post_data_json
            except Exception:
                return
            if post_data == {"limit": LIST_LIMIT, "page": 1}:
                enqueue_json_response(self._list_responses, response)
            return
        self._record_detail_response(response, self._detail_responses)

    @staticmethod
    def _record_detail_response(
        response: Response,
        queue: JsonResponseQueue,
    ) -> None:
        if response.status == 200 and DETAIL_ENDPOINT in response.url:
            enqueue_json_response(queue, response)

    async def _rate_limit(self) -> None:
        async with self._request_start_lock:
            delay = self.settings.qihu360_request_delay_seconds - (
                asyncio.get_running_loop().time() - self._last_request_started_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started_at = asyncio.get_running_loop().time()

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
        raise RuntimeError(f"360 {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"360 {field} is not an integer: {value!r}")
    return value


def _source_date(value: Any, field: str) -> datetime | None:
    text_value = _optional(value)
    if text_value is None:
        return None
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(f"360 {field} is not an ISO date: {text_value!r}") from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))


def _experience_range(value: Any, external_id: str) -> tuple[int | None, int | None]:
    text_value = _optional(value)
    if text_value is None or text_value == "不限":
        return None, None
    match = re.fullmatch(r"(\d+)-(\d+)年", text_value)
    if match is None:
        raise ValueError(
            f"360 job {external_id} has an unknown experience range: {text_value!r}"
        )
    minimum, maximum = (int(item) for item in match.groups())
    if minimum > maximum:
        raise ValueError(f"360 job {external_id} has an inverted experience range")
    return minimum, maximum


def _job_sections(
    raw: dict[str, Any],
    external_id: str,
) -> tuple[str | None, str | None]:
    raw_description = _optional(raw.get("description"))
    raw_qualification = _optional(raw.get("qualification"))
    description = _plain_text(raw_description) if raw_description else None
    qualification = _plain_text(raw_qualification) if raw_qualification else None
    if qualification is not None:
        return description, qualification
    if description is None:
        return None, None

    lines = [line for line in description.splitlines() if line]
    requirement_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _heading_key(line) in REQUIREMENT_HEADINGS
        ),
        None,
    )
    if requirement_index is None:
        return description, None
    duties = "\n".join(lines[:requirement_index]).strip() or None
    requirements = "\n".join(lines[requirement_index + 1 :]).strip() or None
    if duties is None and requirements is None:
        raise ValueError(f"360 job {external_id} has empty text sections")
    return duties, requirements


def _heading_key(value: str) -> str:
    return re.sub(r"[\s:：]+", " ", value).strip(" :：").lower()


def _plain_text(value: str) -> str:
    parser = _SourceTextParser()
    parser.feed(value)
    parser.close()
    text_value = "".join(parser.parts).replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text_value.split("\n")]
    return "\n".join(line for line in lines if line)


class _SourceTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "li", "ol", "p", "section", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
