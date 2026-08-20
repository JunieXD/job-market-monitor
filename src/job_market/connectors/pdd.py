"""PDD public graduate and internship recruitment connector."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil
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
UI_PAGE_SIZE = 10
DETAIL_CONCURRENCY = 2
DETAIL_OPEN_ATTEMPTS = 3
DETAIL_ENDPOINT = "https://careers.pddglobalhr.com/api/careers/api/recruit/position/detail"


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    path: str
    list_endpoint: str
    employment_type_id: str
    employment_type_name: str

    @property
    def page_url(self) -> str:
        return f"https://careers.pddglobalhr.com/campus/{self.path}"

    def detail_url(self, external_id: str) -> str:
        return f"{self.page_url}/detail?positionId={external_id}"


PORTALS = {
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        path="grad",
        list_endpoint="/api/recruit/position/list",
        employment_type_id="grad",
        employment_type_name="应届生招聘",
    ),
    Channel.INTERNSHIP: PortalConfig(
        channel=Channel.INTERNSHIP,
        path="intern",
        list_endpoint="/api/recruit/position/train/list",
        employment_type_id="intern",
        employment_type_name="实习生招聘",
    ),
}


class PDDConnector:
    """Collect complete PDD lists and the detail response for every job."""

    source_key = "pdd_cn"

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
        self._active_list_endpoint: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("PDD connector supports only campus and internship channels")

        payload = await self._open_first_page(portal)
        first = self._list_page(payload, expected_page=1)
        total_count = first["total"]
        total_pages = ceil(total_count / UI_PAGE_SIZE) if total_count else 0
        list_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        complete = True

        if total_pages == 0:
            self._save_list_payload(channel, 1, payload)

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            await self._assert_active_page(page_number)
            current = self._list_page(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_list_payload(channel, page_number, payload)
            for raw in current["rows"]:
                external_id = _required(raw, "id", "PDD list job id")
                if external_id in seen_ids:
                    raise RuntimeError(f"PDD list repeated job id {external_id}")
                seen_ids.add(external_id)
                list_rows.append(raw)
            if page_number < total_pages:
                payload = await self._next_list_page(page_number + 1)

        if complete and len(list_rows) != total_count:
            raise RuntimeError(
                "PDD pagination count mismatch: "
                f"declared={total_count}, unique={len(list_rows)}"
            )

        detail_results = await self._collect_details(portal, list_rows)
        jobs: list[JobRecord] = []
        for index, (record, detail_payload) in enumerate(detail_results):
            self._save_detail_payload(channel, record.external_id, index, detail_payload)
            jobs.append(record)

        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts={"all": total_count},
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(self, portal: PortalConfig) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._list_responses)
        self._active_list_endpoint = portal.list_endpoint
        await self.page.goto(
            portal.page_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        return await self._next_result(self._list_responses, "positions:1")

    async def _next_list_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._list_responses)
        next_button = self.page.locator(".rocket-pagination-next")
        if await next_button.count() != 1:
            raise RuntimeError("PDD pagination has no unique next-page control")
        classes = await next_button.get_attribute("class") or ""
        aria_disabled = await next_button.get_attribute("aria-disabled")
        if "rocket-pagination-disabled" in classes or aria_disabled == "true":
            raise RuntimeError(f"PDD pagination ended before page {page_number}")
        await next_button.click()
        payload = await self._next_result(
            self._list_responses,
            f"positions:{page_number}",
        )
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".rocket-pagination-item-active")
        if await active.count() != 1:
            raise RuntimeError("PDD pagination has no unique active page")
        text = (await active.inner_text()).strip()
        if text != str(expected_page):
            raise RuntimeError(
                f"PDD page mismatch: expected={expected_page}, got={text!r}"
            )

    async def _collect_details(
        self,
        portal: PortalConfig,
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
                external_id = _required(list_row, "id", "PDD list job id")
                detail_payload = await self._open_detail(
                    page,
                    queue,
                    portal,
                    external_id,
                )
                detail = detail_payload["result"]
                record = self.parse_job(list_row, detail, portal)
                results[index] = (record, detail_payload)

        try:
            await asyncio.gather(*(worker(page, queue) for page, queue in workers))
        finally:
            await asyncio.gather(
                *(page.close() for page in extra_pages),
                return_exceptions=True,
            )

        if any(result is None for result in results):
            raise RuntimeError("PDD detail collection finished with missing results")
        return [result for result in results if result is not None]

    async def _open_detail(
        self,
        page: Page,
        queue: JsonResponseQueue,
        portal: PortalConfig,
        external_id: str,
    ) -> dict[str, Any]:
        for attempt in range(1, DETAIL_OPEN_ATTEMPTS + 1):
            try:
                await self._rate_limit()
                # The detail route only bootstraps this same-origin JSON POST.
                # Keep the browser context for cookies while avoiding one HTML
                # and JavaScript bundle download per job.
                response = await page.request.post(
                    DETAIL_ENDPOINT,
                    data={"id": external_id, "t": None},
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": portal.detail_url(external_id),
                    },
                    timeout=60_000,
                )
                if response.status != 200:
                    raise BrowserResponseUnavailableError(
                        f"PDD detail API returned HTTP {response.status}"
                    )
                payload = await response.json()
                if (
                    not isinstance(payload, dict)
                    or payload.get("success") is not True
                    or not isinstance(payload.get("result"), dict)
                ):
                    raise BrowserResponseUnavailableError(
                        f"PDD detail API returned an invalid response for {external_id}"
                    )
                actual = _optional(payload["result"].get("id"))
                if actual != external_id:
                    raise RuntimeError(
                        "PDD detail response mismatch: "
                        f"requested={external_id}, got={actual!r}"
                    )
                return payload
            except BrowserResponseUnavailableError as exc:
                if attempt == DETAIL_OPEN_ATTEMPTS:
                    raise BrowserResponseUnavailableError(
                        f"PDD detail {external_id} did not load after "
                        f"{DETAIL_OPEN_ATTEMPTS} attempts"
                    ) from exc
        raise AssertionError("unreachable")

    async def _next_result(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"PDD response: {operation}",
        )
        if payload.get("success") is not True or not isinstance(
            payload.get("result"), dict
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid PDD response for {operation}: {message}")
        return payload

    @staticmethod
    def _list_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("PDD list response result is not an object")
        rows = result.get("list")
        total = _response_int(result.get("total"), "total", allow_zero=True)
        if not isinstance(rows, list):
            raise RuntimeError("PDD response has no job list")
        expected_rows = min(
            UI_PAGE_SIZE,
            max(total - (expected_page - 1) * UI_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"PDD page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"PDD page {expected_page} contains a non-object row"
                )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any],
        portal: PortalConfig,
    ) -> JobRecord:
        external_id = _required(list_raw, "id", "PDD list job id")
        detail_id = _required(detail_raw, "id", "PDD detail job id")
        if detail_id != external_id:
            raise ValueError(
                f"PDD job identity changed between list and detail: "
                f"{external_id!r} != {detail_id!r}"
            )

        for field in (
            "name",
            "workLocationName",
            "job",
            "jobName",
            "releaseTime",
            "jobDuty",
            "recruitTypeName",
            "graduationYear",
        ):
            if list_raw.get(field) != detail_raw.get(field):
                raise ValueError(
                    f"PDD job {external_id} changed field {field!r} "
                    "between list and detail"
                )

        title = _required(detail_raw, "name", f"PDD job title ({external_id})")
        description = _required(
            detail_raw,
            "jobDuty",
            f"PDD job description ({external_id})",
        )
        requirements = _required(
            detail_raw,
            "serveRequirement",
            f"PDD job requirements ({external_id})",
        )
        category_code = _required(
            detail_raw,
            "job",
            f"PDD job category ({external_id})",
        )
        category_name = _required(
            detail_raw,
            "jobName",
            f"PDD job category name ({external_id})",
        )
        location_code = _required(
            list_raw,
            "workLocation",
            f"PDD job location code ({external_id})",
        )
        location_name = _required(
            detail_raw,
            "workLocationName",
            f"PDD job location name ({external_id})",
        )
        labels = list_raw.get("labelList")
        if not isinstance(labels, list) or any(
            not isinstance(label, str) or not label.strip() for label in labels
        ):
            raise ValueError(f"PDD job {external_id} has invalid source labels")

        return JobRecord(
            source_key="pdd_cn",
            external_id=external_id,
            external_code=_optional(list_raw.get("code")),
            source_url=portal.detail_url(external_id),
            company_name="拼多多集团",
            channel=portal.channel,
            employment_type_id=portal.employment_type_id,
            employment_type_name=portal.employment_type_name,
            recruitment_project_name=_optional(detail_raw.get("recruitTypeName")),
            title=title,
            description=description,
            requirements=requirements,
            published_at=_timestamp_ms(detail_raw.get("releaseTime"), "releaseTime"),
            source_status=_optional(detail_raw.get("voteArrange")),
            locations=[LocationRecord(code=location_code, name=location_name)],
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload={"list": list_raw, "detail": detail_raw},
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if (
            self._active_list_endpoint is not None
            and self._active_list_endpoint in response.url
        ):
            enqueue_json_response(self._list_responses, response)
            return
        self._record_detail_response(response, self._detail_responses)

    @staticmethod
    def _record_detail_response(
        response: Response,
        queue: JsonResponseQueue,
    ) -> None:
        if (
            response.status == 200
            and "/api/recruit/position/detail" in response.url
            and "/detail/type" not in response.url
            and "/detail/train/type" not in response.url
        ):
            enqueue_json_response(queue, response)

    async def _rate_limit(self) -> None:
        async with self._request_start_lock:
            delay = self.settings.pdd_request_delay_seconds - (
                asyncio.get_running_loop().time() - self._last_request_started_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started_at = asyncio.get_running_loop().time()

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
                    partition="list",
                    offset=page_number - 1,
                    payload=payload,
                )
            )

    def _save_detail_payload(
        self,
        channel: Channel,
        external_id: str,
        offset: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition=f"detail-{external_id}",
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
    if isinstance(value, bool):
        raise RuntimeError(f"PDD {field} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"PDD {field} is not an integer: {value!r}") from exc
    if str(parsed) != str(value).strip():
        raise RuntimeError(f"PDD {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        raise RuntimeError(f"PDD {field} is not an integer: {value!r}")
    return parsed


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"PDD {field} is not a millisecond timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))
