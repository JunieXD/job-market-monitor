"""JD.com public experienced-recruitment connector."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.browser_json import (
    JsonResponseQueue,
    drain_json_responses,
    enqueue_json_response,
    next_json_response,
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
POSITION_LIST_URL = "https://zhaopin.jd.com/web/job/job_info_list/3"


class JDConnector:
    """Collect JD's public list response through the rendered list page."""

    source_key = "jd_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("JD connector supports only the experienced channel")

        payload = await self._open_first_page()
        total_pages = await self._read_total_pages()
        jobs_by_id: dict[str, JobRecord] = {}
        complete = True

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            await self._assert_active_page(page_number)
            rows = self._validate_rows(payload, page_number)
            self.pages_fetched += 1
            self._save_payload(channel, page_number, payload)
            for raw in rows:
                record = self.parse_job(raw)
                previous = jobs_by_id.get(record.external_id)
                if previous is None:
                    jobs_by_id[record.external_id] = record
                else:
                    jobs_by_id[record.external_id] = _merge_locations(previous, record)

            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if complete and not jobs_by_id:
            raise RuntimeError("JD returned no jobs for a non-empty pagination range")

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts={"all": len(jobs_by_id)},
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(self) -> list[dict[str, Any]]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_payload("positions:1")
        return self._validate_rows(payload, 1)

    async def _next_page(self, page_number: int) -> list[dict[str, Any]]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_link = self.page.locator("a.next")
        if await next_link.count() != 1:
            raise RuntimeError("JD pagination has no unique next link")
        if "disabled" in (await next_link.get_attribute("class") or ""):
            raise RuntimeError(f"JD pagination ended before page {page_number}")
        await next_link.click()
        payload = await self._next_payload(f"positions:{page_number}")
        return self._validate_rows(payload, page_number)

    async def _read_total_pages(self) -> int:
        next_link = self.page.locator("a.next")
        if await next_link.count() != 1:
            raise RuntimeError("JD pagination has no unique next link")
        pages = await next_link.evaluate(
            """node => [...node.parentElement.querySelectorAll('a')]
                .map(item => Number(item.textContent.trim()))
                .filter(value => Number.isInteger(value) && value > 0)"""
        )
        if not pages:
            raise RuntimeError("JD pagination exposed no numeric page count")
        total_pages = max(pages)
        if total_pages < 1:
            raise RuntimeError(f"JD returned invalid page count: {total_pages}")
        return total_pages

    async def _assert_active_page(self, expected: int) -> None:
        active = self.page.locator("a.active, li.active")
        if await active.count() == 0:
            return
        text = (await active.first.inner_text()).strip()
        if text.isdigit() and int(text) != expected:
            raise RuntimeError(f"JD page mismatch: expected={expected}, got={text}")

    async def _next_payload(self, operation: str) -> Any:
        payload = await next_json_response(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"JD response: {operation}",
        )
        if not isinstance(payload, list):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid JD position response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _validate_rows(payload: Any, page_number: int) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise RuntimeError(f"JD page {page_number} is not a list")
        rows: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, dict):
                raise RuntimeError(f"JD page {page_number} contains a non-object row")
            rows.append(row)
        if not rows:
            raise RuntimeError(f"JD returned an empty page {page_number}")
        return rows

    def _record_response(self, response: Response) -> None:
        if response.status == 200 and "/web/job/job_list" in response.url:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.jd_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _save_payload(self, channel: Channel, page_number: int, payload: Any) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="all",
                    offset=page_number - 1,
                    payload=payload,
                )
            )

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "requirementId", "JD job id")
        title = _required(raw, "positionNameOpen", f"JD job title ({external_id})")
        description = _required(raw, "workContent", f"JD job content ({external_id})")
        requirements = _required(raw, "qualification", f"JD job qualification ({external_id})")
        category_name = _optional(raw.get("jobType"))
        category_code = _optional(raw.get("jobTypeCode"))
        categories = []
        if category_name is not None:
            categories.append(
                SourceCategoryRecord(
                    external_id=category_code or f"jobType:{category_name}",
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            )

        department_name = _optional(raw.get("positionDeptName"))
        business_units = []
        if department_name is not None:
            business_units.append(
                BusinessUnitRecord(
                    code=_optional(raw.get("positionDeptCode"))
                    or f"business:{department_name}",
                    name=department_name,
                )
            )

        city_name = _required(raw, "workCity", f"JD work city ({external_id})")
        city_code = _optional(raw.get("workCityCode")) or f"city:{city_name}"
        return JobRecord(
            source_key="jd_cn",
            external_id=external_id,
            external_code=_optional(raw.get("positionCode")),
            source_url=f"{POSITION_LIST_URL}?requirementId={external_id}",
            company_name="京东集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=title,
            description=description,
            requirements=requirements,
            published_at=_timestamp_ms(raw.get("publishTime"), "publishTime"),
            is_hot=_as_bool(raw.get("isHot")),
            locations=[LocationRecord(code=city_code, name=city_name)],
            categories=categories,
            business_units=business_units,
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


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"JD {field} is not a numeric timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    raise ValueError(f"JD isHot is not boolean-like: {value!r}")


def _merge_locations(previous: JobRecord, current: JobRecord) -> JobRecord:
    previous_comparable = previous.model_dump(
        mode="json", exclude={"locations", "source_payload"}
    )
    current_comparable = current.model_dump(
        mode="json", exclude={"locations", "source_payload"}
    )
    if previous_comparable != current_comparable:
        raise RuntimeError(
            f"JD duplicated requirement {previous.external_id} with conflicting content"
        )
    locations = list(previous.locations)
    known = {location.code for location in locations}
    for location in current.locations:
        if location.code not in known:
            locations.append(location)
            known.add(location.code)
    return previous.model_copy(update={"locations": locations})
