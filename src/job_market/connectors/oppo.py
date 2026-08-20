"""OPPO public experienced-recruitment connector."""

import asyncio
import json
import re
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
POSITION_LIST_URL = (
    "https://career.oppo.com/official/oppo/recruitment/post"
    "?recruitType=SOCIAL-RECRUITMENT"
)
POSITION_ENDPOINT = "/ats-candidate-api/open-api/position/queryPositionList"
DICTIONARY_ENDPOINT = "/ats-candidate-api/open-api/enum/dictionaries"


class OppoConnector:
    """Collect OPPO social jobs and the source dictionaries used by the UI."""

    source_key = "oppo_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._degree_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("OPPO connector supports only the experienced channel")

        payload, category_payload, degree_payload = await self._open_first_page()
        category_names = self.parse_dictionary(category_payload, "JOB-TYPE")
        degree_names = self.parse_dictionary(degree_payload, "EDUCATION-REQUIRE")
        self._save_payload(channel, "category-dictionary", 0, category_payload)
        self._save_payload(channel, "degree-dictionary", 0, degree_payload)

        first = self._position_page(payload, expected_page=1)
        total_count = first["total"]
        total_pages = ceil(total_count / UI_PAGE_SIZE) if total_count else 0
        jobs_by_id: dict[str, JobRecord] = {}
        complete = True

        if total_pages == 0:
            self._save_payload(channel, "list", 0, payload)

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            await self._assert_active_page(page_number)
            current = self._position_page(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_payload(channel, "list", page_number - 1, payload)
            for raw in current["rows"]:
                record = self.parse_job(raw, category_names, degree_names)
                previous = jobs_by_id.get(record.external_id)
                if previous is not None:
                    if previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"OPPO returned conflicting job {record.external_id}"
                        )
                    raise RuntimeError(f"OPPO repeated job {record.external_id}")
                jobs_by_id[record.external_id] = record
            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if complete and len(jobs_by_id) != total_count:
            raise RuntimeError(
                "OPPO pagination count mismatch: "
                f"declared={total_count}, unique={len(jobs_by_id)}"
            )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts={"all": total_count},
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        drain_json_responses(self._category_responses)
        drain_json_responses(self._degree_responses)
        self._active_page = 1
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload, category_payload, degree_payload = await asyncio.gather(
            self._next_success(self._position_responses, "positions:1"),
            self._next_success(self._category_responses, "JOB-TYPE dictionary"),
            self._next_success(
                self._degree_responses,
                "EDUCATION-REQUIRE dictionary",
            ),
        )
        await self._assert_active_page(1)
        return payload, category_payload, degree_payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_button = self.page.locator('button[aria-label="下一页"]')
        if await next_button.count() != 1:
            raise RuntimeError("OPPO pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(f"OPPO pagination ended before page {page_number}")
        await next_button.click()
        payload = await self._next_success(
            self._position_responses,
            f"positions:{page_number}",
        )
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".el-pagination .el-pager .is-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"OPPO page did not render active page {expected_page}")

    async def _next_success(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"OPPO response: {operation}",
        )
        if payload.get("code") != "0":
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid OPPO response for {operation}: {message}")
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
            raise RuntimeError("OPPO response has no data object")
        rows = data.get("list")
        total = _response_int(data.get("total"), "total", allow_zero=True)
        page_number = _response_int(data.get("pageNum"), "pageNum")
        page_size = _response_int(data.get("pageSize"), "pageSize")
        pages = _response_int(data.get("pages"), "pages", allow_zero=True)
        if page_number != expected_page:
            raise RuntimeError(
                f"OPPO page mismatch: expected={expected_page}, got={page_number}"
            )
        if page_size != UI_PAGE_SIZE:
            raise RuntimeError(
                f"OPPO page size changed: expected={UI_PAGE_SIZE}, got={page_size}"
            )
        expected_pages = ceil(total / page_size) if total else 0
        if pages != expected_pages:
            raise RuntimeError(
                f"OPPO page count mismatch: expected={expected_pages}, got={pages}"
            )
        if not isinstance(rows, list):
            raise RuntimeError("OPPO response has no position list")
        expected_rows = min(
            page_size,
            max(total - (page_number - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"OPPO page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"OPPO page {page_number} has a non-object row")
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_dictionary(
        payload: dict[str, Any],
        expected_type: str,
    ) -> dict[str, str]:
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"OPPO {expected_type} dictionary is empty")
        values: dict[str, str] = {}
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("dictType") != expected_type:
                raise RuntimeError(f"OPPO {expected_type} dictionary has an invalid row")
            code = _required(raw, "dictValue", f"OPPO {expected_type} code")
            name = _required(raw, "dictNameCn", f"OPPO {expected_type} name")
            if code in values:
                raise RuntimeError(f"OPPO {expected_type} dictionary repeats {code!r}")
            values[code] = name
        return values

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        category_names: dict[str, str],
        degree_names: dict[str, str],
    ) -> JobRecord:
        external_id = _required(raw, "positionId", "OPPO job id")
        title = _required(raw, "publishName", f"OPPO job title ({external_id})")
        category_code = _required(
            raw,
            "jobType",
            f"OPPO job category ({external_id})",
        )
        category_name = category_names.get(category_code)
        if category_name is None:
            raise ValueError(
                f"OPPO job {external_id} has unknown category {category_code!r}"
            )
        degree_code = _optional(raw.get("educationRequire"))
        degree_name = degree_names.get(degree_code) if degree_code else None
        if degree_code is not None and degree_name is None:
            raise ValueError(
                f"OPPO job {external_id} has unknown degree {degree_code!r}"
            )
        if raw.get("recruitType") != "SOCIAL-RECRUITMENT":
            raise ValueError(f"OPPO job {external_id} is not a social job")
        locations = _locations(raw, external_id)
        experience_min = _optional_non_negative_int(
            raw.get("minWorkYears"),
            "minWorkYears",
            external_id,
        )
        experience_max = _optional_non_negative_int(
            raw.get("maxWorkYears"),
            "maxWorkYears",
            external_id,
        )

        return JobRecord(
            source_key="oppo_social_cn",
            external_id=external_id,
            external_code=_optional(raw.get("jobCode")),
            source_url=POSITION_LIST_URL,
            company_name="OPPO",
            channel=Channel.EXPERIENCED,
            employment_type_id="SOCIAL-RECRUITMENT",
            employment_type_name=_required(
                raw,
                "recruitTypeName",
                f"OPPO job recruitment type ({external_id})",
            ),
            title=title,
            description=_optional(raw.get("jobDuty")),
            requirements=_optional(raw.get("workRequire")),
            published_at=_source_date(raw.get("publishDate"), "publishDate"),
            degree_code=degree_code,
            degree_name=degree_name,
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            locations=locations,
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if POSITION_ENDPOINT in response.url:
            try:
                post_data = response.request.post_data_json
            except Exception:
                return
            if not isinstance(post_data, dict):
                return
            expected = {
                "pageNum": self._active_page,
                "pageSize": UI_PAGE_SIZE,
                "publishName": "",
                "workCityCodeList": [],
                "jobTypeList": [],
                "recruitTypeList": ["SOCIAL-RECRUITMENT"],
                "shareId": "",
            }
            if post_data == expected:
                enqueue_json_response(self._position_responses, response)
            return
        if DICTIONARY_ENDPOINT not in response.url:
            return
        query = response.url.partition("?")[2]
        if query == "dictTypes=JOB-TYPE":
            enqueue_json_response(self._category_responses, response)
        elif query == "dictTypes=EDUCATION-REQUIRE":
            enqueue_json_response(self._degree_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.oppo_request_delay_seconds - (
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
    if isinstance(value, bool):
        raise RuntimeError(f"OPPO {field} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"OPPO {field} is not an integer: {value!r}") from exc
    if str(parsed) != str(value).strip():
        raise RuntimeError(f"OPPO {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        raise RuntimeError(f"OPPO {field} is invalid: {value!r}")
    return parsed


def _optional_non_negative_int(
    value: Any,
    field: str,
    external_id: str,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"OPPO job {external_id} {field} is invalid: {value!r}")
    return value


def _source_date(value: Any, field: str) -> datetime | None:
    text_value = _optional(value)
    if text_value is None:
        return None
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(f"OPPO {field} is not an ISO date: {text_value!r}") from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))


def _locations(raw: dict[str, Any], external_id: str) -> list[LocationRecord]:
    codes = _split_source_list(raw.get("workCityCode"))
    names = _split_source_list(raw.get("workCityName"))
    if not codes or not names or len(codes) != len(names):
        raise ValueError(f"OPPO job {external_id} has invalid work-city fields")
    if len(set(codes)) != len(codes):
        raise ValueError(f"OPPO job {external_id} has duplicate city codes")
    return [
        LocationRecord(code=code, name=name)
        for code, name in zip(codes, names, strict=True)
    ]


def _split_source_list(value: Any) -> list[str]:
    text_value = _optional(value)
    if text_value is None:
        return []
    return [item.strip() for item in re.split(r"[,，]", text_value) if item.strip()]
