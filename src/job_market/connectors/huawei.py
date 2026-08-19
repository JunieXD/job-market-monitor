"""Huawei public campus and experienced recruitment connector."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

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
PORTAL_OPEN_ATTEMPTS = 3


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    job_type: str
    record_job_type: str
    url: str
    employment_type_name: str


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        job_type="SR",
        record_job_type="1",
        url="https://career.huawei.com/cn/social-recruitment-job-list",
        employment_type_name="社会招聘",
    ),
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        job_type="CR",
        record_job_type="0",
        url="https://career.huawei.com/cn/campus-recruitment-job-list",
        employment_type_name="校园招聘",
    ),
}


class HuaweiConnector:
    """Collect Huawei jobs through the official paginated list controls."""

    source_key = "huawei_cn"

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
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Huawei connector does not support this channel")

        payload = await self._open_first_page(portal)
        first = self._page_data(payload, expected_page=1)
        total_pages = first["pages"]
        total_count = first["total"]
        jobs_by_id: dict[str, JobRecord] = {}
        source_ids: set[str] = set()
        excluded_incomplete_count = 0
        complete = True

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            current = self._page_data(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_payload(channel, page_number, payload)
            for raw in current["rows"]:
                external_id = _required(
                    raw,
                    "advertisementsIntegrationId",
                    "Huawei advertisement id",
                )
                if external_id in source_ids:
                    raise RuntimeError(f"Huawei repeated source record {external_id}")
                source_ids.add(external_id)
                if _missing_required_source_fields(raw):
                    excluded_incomplete_count += 1
                    continue
                record = self.parse_job(raw, portal)
                jobs_by_id[record.external_id] = record
            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if complete and len(source_ids) != total_count:
            raise RuntimeError(
                "Huawei pagination count mismatch: "
                f"declared={total_count}, unique={len(source_ids)}"
            )

        partition_counts = {
            "all": total_count,
            "selected-complete-records": len(jobs_by_id),
        }
        if excluded_incomplete_count:
            partition_counts["excluded-incomplete-source-records"] = (
                excluded_incomplete_count
            )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(self, portal: PortalConfig) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, PORTAL_OPEN_ATTEMPTS + 1):
            await self._rate_limit()
            drain_json_responses(self._position_responses)
            try:
                await self.page.goto(
                    portal.url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                return await self._next_payload("positions:1")
            except (PlaywrightTimeoutError, BrowserResponseUnavailableError) as exc:
                last_error = exc
                if attempt == PORTAL_OPEN_ATTEMPTS:
                    raise
        raise RuntimeError("Huawei portal open attempts exhausted") from last_error

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.locator("button:has(.aui-icon-chevron-right)")
        if await next_button.count() != 1:
            raise RuntimeError("Huawei pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(f"Huawei pagination ended before page {page_number}")
        await next_button.click()
        return await self._next_payload(f"positions:{page_number}")

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Huawei response: {operation}",
        )
        if payload.get("status") != "SUCCESS" or not isinstance(
            payload.get("data"), dict
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Huawei response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _page_data(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Huawei response data is not an object")
        page = data.get("pageVO")
        rows = data.get("result")
        if not isinstance(page, dict) or not isinstance(rows, list):
            raise RuntimeError("Huawei response has no page metadata or job list")
        page_number = _response_int(page.get("curPage"), "curPage")
        page_size = _response_int(page.get("pageSize"), "pageSize")
        total = _response_int(page.get("totalRows"), "totalRows", allow_zero=True)
        pages = _response_int(page.get("totalPages"), "totalPages", allow_zero=True)
        if page_number != expected_page:
            raise RuntimeError(
                f"Huawei page mismatch: expected={expected_page}, got={page_number}"
            )
        if pages != (total + page_size - 1) // page_size:
            raise RuntimeError(
                "Huawei pagination metadata is inconsistent: "
                f"pages={pages}, total={total}, size={page_size}"
            )
        expected_rows = min(page_size, max(total - (page_number - 1) * page_size, 0))
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Huawei page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"Huawei page {page_number} contains a non-object row")
        return {"rows": rows, "pages": pages, "total": total}

    def _record_response(self, response: Response) -> None:
        if response.status == 200 and "/recruitmentPosition/pub/getJobPage" in response.url:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.huawei_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _save_payload(
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

    @staticmethod
    def parse_job(raw: dict[str, Any], portal: PortalConfig) -> JobRecord:
        external_id = _required(
            raw,
            "advertisementsIntegrationId",
            "Huawei advertisement id",
        )
        title = _optional(raw.get("jobName")) or _required(
            raw,
            "jobNameNew",
            f"Huawei job title ({external_id})",
        )
        description = _required(
            raw,
            "mainBusiness",
            f"Huawei job description ({external_id})",
        )
        requirements = _required(
            raw,
            "jobRequire",
            f"Huawei job requirements ({external_id})",
        )
        raw_job_type = _required(raw, "jobType", f"Huawei job type ({external_id})")
        if raw_job_type != portal.record_job_type:
            raise ValueError(
                f"Huawei job {external_id} has type {raw_job_type!r}; "
                f"expected {portal.record_job_type!r}"
            )

        category_name = _optional(raw.get("categoryName")) or _optional(
            raw.get("jobFamilyName")
        )
        category_code = _optional(raw.get("category"))
        categories = []
        if category_name is not None:
            categories.append(
                SourceCategoryRecord(
                    external_id=category_code or f"category:{category_name}",
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            )

        scenario_id = _optional(raw.get("recruitScenarioId"))
        scenario_name = _optional(raw.get("scenarioName"))
        has_named_scenario = scenario_id is not None and scenario_name is not None
        employment_type_id = scenario_id if has_named_scenario else raw_job_type
        employment_type_name = scenario_name or portal.employment_type_name
        return JobRecord(
            source_key="huawei_cn",
            external_id=external_id,
            external_code=_optional(raw.get("jobId")),
            source_url=portal.url,
            company_name="华为",
            channel=portal.channel,
            employment_type_id=employment_type_id,
            employment_type_name=employment_type_name,
            recruitment_project_id=scenario_id if has_named_scenario else None,
            recruitment_project_name=scenario_name if has_named_scenario else None,
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_date(raw.get("releaseDate"), "releaseDate"),
            source_updated_at=_source_date(
                raw.get("lastUpdateDate"),
                "lastUpdateDate",
            ),
            source_status=_optional(raw.get("upLineStatus"))
            or _optional(raw.get("cancelflag")),
            degree_name=_optional(raw.get("degree")),
            experience_min_years=_optional_years(raw.get("workYear")),
            department_code=_optional(raw.get("deptCode")),
            department_name=_optional(raw.get("deptName")),
            locations=_locations(raw.get("workPlace"), external_id),
            categories=categories,
            business_units=_business_units(raw),
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


def _missing_required_source_fields(raw: dict[str, Any]) -> tuple[str, ...]:
    missing = []
    if _optional(raw.get("jobName")) is None and _optional(
        raw.get("jobNameNew")
    ) is None:
        missing.append("jobName/jobNameNew")
    for key in ("mainBusiness", "jobRequire", "workPlace"):
        if _optional(raw.get(key)) is None:
            missing.append(key)
    return tuple(missing)


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Huawei {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Huawei {field} is not an integer: {value!r}")
    return value


def _source_date(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Huawei {field} is not an ISO date: {text!r}") from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))


def _optional_years(value: Any) -> int | None:
    text = _optional(value)
    if text is None or not text.isdigit():
        return None
    return int(text)


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    text = _optional(value)
    if text is None:
        raise ValueError(f"Huawei job has no work location: {external_id}")
    names = [item.strip() for item in re.split(r"[/,，]", text) if item.strip()]
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError(f"Huawei job has no work location: {external_id}")
    return [LocationRecord(code=f"city:{name}", name=name) for name in names]


def _business_units(raw: dict[str, Any]) -> list[BusinessUnitRecord]:
    units: list[BusinessUnitRecord] = []
    known_names: set[str] = set()
    department_names = _optional(raw.get("deptName"))
    if department_names is not None:
        for name in re.split(r"[,，]", department_names):
            name = name.strip()
            if name and name not in known_names:
                known_names.add(name)
                units.append(BusinessUnitRecord(code=f"department:{name}", name=name))
    first_name = _optional(raw.get("firstDeptName"))
    if first_name is not None and first_name not in known_names:
        units.append(
            BusinessUnitRecord(
                code=_optional(raw.get("firstDeptCode"))
                or f"first-department:{first_name}",
                name=first_name,
            )
        )
    return units
