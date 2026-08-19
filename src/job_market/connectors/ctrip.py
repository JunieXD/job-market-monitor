"""Ctrip public mixed experienced-page recruitment connector."""

import asyncio
import re
from datetime import date, datetime, time
from html.parser import HTMLParser
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page, Response, Route

from job_market.config import Settings
from job_market.connectors.browser_json import (
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
INITIAL_PAGE_SIZE = 10
COLLECTION_PAGE_SIZE = 20
POSITION_LIST_URL = "https://job.ctrip.com/#/experienced/jobList"
POSITION_DETAIL_URL = "https://job.ctrip.com/#/experienced/job-detail/{code}"

RESPONSIBILITY_HEADINGS = {
    "职位描述",
    "岗位职责",
    "工作职责",
    "工作描述",
    "岗位描述",
    "职位职责",
    "主要职责",
    "核心职责",
    "工作内容",
    "job responsibilities",
    "core responsibilities",
    "responsibilities",
}
REQUIREMENT_HEADINGS = {
    "任职资格",
    "任职要求",
    "职位要求",
    "岗位要求",
    "基本录用要求",
    "基本要求",
    "录用要求",
    "职位资格",
    "任职条件",
    "key qualifications and experience",
    "job qualifications",
    "qualifications",
    "requirements",
}


class CtripConnector:
    """Collect the public list that mixes full-time and internship jobs."""

    source_key = "ctrip_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self._active_page_size = INITIAL_PAGE_SIZE
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.GENERAL:
            raise ValueError("Ctrip connector supports only the general channel")

        await self.page.route("**/*", _skip_nonessential_assets)
        payload = await self._open_collection_page()
        first = self._position_page(
            payload,
            expected_page=1,
            page_size=COLLECTION_PAGE_SIZE,
        )
        total_count = first["total"]
        total_pages = ceil(total_count / COLLECTION_PAGE_SIZE) if total_count else 0
        jobs_by_id: dict[str, JobRecord] = {}
        complete = True

        if total_pages == 0:
            self._save_payload(channel, 1, payload)

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            await self._assert_active_page(page_number)
            current = self._position_page(
                payload,
                expected_page=page_number,
                page_size=COLLECTION_PAGE_SIZE,
            )
            self.pages_fetched += 1
            self._save_payload(channel, page_number, payload)
            for raw in current["rows"]:
                record = self.parse_job(raw)
                previous = jobs_by_id.get(record.external_id)
                if previous is not None:
                    if previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Ctrip returned conflicting job {record.external_id}"
                        )
                    raise RuntimeError(f"Ctrip repeated job {record.external_id}")
                jobs_by_id[record.external_id] = record
            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if complete and len(jobs_by_id) != total_count:
            raise RuntimeError(
                "Ctrip pagination count mismatch: "
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

    async def _open_collection_page(self) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = 1
        self._active_page_size = INITIAL_PAGE_SIZE
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        initial = await self._next_payload("positions:initial:1")
        self._position_page(
            initial,
            expected_page=1,
            page_size=INITIAL_PAGE_SIZE,
        )

        size_control = self.page.locator(".ant-pagination-options-size-changer")
        if await size_control.count() != 1:
            raise RuntimeError("Ctrip pagination has no unique page-size control")
        await size_control.click()
        size_option = self.page.locator(
            ".ant-select-item-option",
            has_text=f"{COLLECTION_PAGE_SIZE} 条/页",
        )
        if await size_option.count() != 1:
            raise RuntimeError(
                f"Ctrip page-size menu has no {COLLECTION_PAGE_SIZE}-row option"
            )

        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page_size = COLLECTION_PAGE_SIZE
        await size_option.click()
        payload = await self._next_payload("positions:collection:1")
        await self._assert_active_page(1)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_button = self.page.locator(".ant-pagination-next")
        if await next_button.count() != 1:
            raise RuntimeError("Ctrip pagination has no unique next-page control")
        classes = await next_button.get_attribute("class") or ""
        aria_disabled = await next_button.get_attribute("aria-disabled")
        if "ant-pagination-disabled" in classes or aria_disabled == "true":
            raise RuntimeError(f"Ctrip pagination ended before page {page_number}")
        button = next_button.locator("button")
        await (button if await button.count() == 1 else next_button).click()
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".ant-pagination-item-active")
        if await active.count() != 1:
            raise RuntimeError("Ctrip pagination has no unique active page")
        text = (await active.inner_text()).strip()
        if text != str(expected_page):
            raise RuntimeError(
                f"Ctrip page mismatch: expected={expected_page}, got={text!r}"
            )

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Ctrip response: {operation}",
        )
        if payload.get("retCode") != "201" or not isinstance(
            payload.get("retValue"), dict
        ):
            raise RuntimeError(f"Invalid Ctrip response for {operation}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
        page_size: int,
    ) -> dict[str, Any]:
        result = payload.get("retValue")
        if not isinstance(result, dict):
            raise RuntimeError("Ctrip response result is not an object")
        rows = result.get("recruitJobAdList")
        total = _response_int(result.get("total"), "total", allow_zero=True)
        if not isinstance(rows, list):
            raise RuntimeError("Ctrip response has no job list")
        expected_rows = min(
            page_size,
            max(total - (expected_page - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Ctrip page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Ctrip page {expected_page} contains a non-object row"
                )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "id", "Ctrip job id")
        external_code = _required(raw, "fromId", f"Ctrip job code ({external_id})")
        title = _required(raw, "jobTitle", f"Ctrip job title ({external_id})")
        description, requirements = _job_sections(raw, external_id)
        location_code = _optional(raw.get("city"))
        location_name = _optional(raw.get("cityName"))
        if (location_code is None) != (location_name is None):
            raise ValueError(
                f"Ctrip job {external_id} has an incomplete structured city"
            )
        category_code = _required(
            raw,
            "jobFamilyGroupCode",
            f"Ctrip job category code ({external_id})",
        )
        category_name = _required(
            raw,
            "jobFamilyGroupName",
            f"Ctrip job category name ({external_id})",
        )
        business_code = _optional(raw.get("buCode"))
        business_name = _optional(raw.get("buName"))

        return JobRecord(
            source_key="ctrip_cn",
            external_id=external_id,
            external_code=external_code,
            source_url=POSITION_DETAIL_URL.format(code=external_code),
            company_name="携程集团",
            channel=Channel.GENERAL,
            employment_type_id="experienced-page-mixed",
            employment_type_name="社会招聘页混合岗位",
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_date(raw.get("publishDate"), "publishDate"),
            locations=(
                []
                if location_code is None or location_name is None
                else [LocationRecord(code=location_code, name=location_name)]
            ),
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            business_units=(
                []
                if business_code is None or business_name is None
                else [BusinessUnitRecord(code=business_code, name=business_name)]
            ),
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200 or "/api/hrrecruit/getJobAd" not in response.url:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        if not isinstance(post_data, dict):
            return
        condition = post_data.get("condition")
        pager = post_data.get("pager")
        if not isinstance(condition, dict) or not isinstance(pager, dict):
            return
        if condition.get("category") != 1:
            return
        if _request_int(pager.get("index")) != self._active_page:
            return
        if _request_int(pager.get("size")) != self._active_page_size:
            return
        enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.ctrip_request_delay_seconds - (
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
                    partition="experienced-page-mixed",
                    offset=page_number - 1,
                    payload=payload,
                )
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


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Ctrip {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Ctrip {field} is not an integer: {value!r}")
    return value


def _request_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if str(parsed) == str(value).strip() else None


def _source_date(value: Any, field: str) -> datetime | None:
    text_value = _optional(value)
    if text_value is None:
        return None
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(f"Ctrip {field} is not an ISO date: {text_value!r}") from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))


def _job_sections(
    raw: dict[str, Any],
    external_id: str,
) -> tuple[str | None, str | None]:
    raw_duty = _optional(raw.get("duty"))
    raw_requirements = _required(
        raw,
        "requirements",
        f"Ctrip job body ({external_id})",
    )
    if raw_duty is not None:
        description = _plain_text(raw_duty)
        requirements = _plain_text(raw_requirements)
        if not description or not requirements:
            raise ValueError(f"Ctrip job {external_id} has empty text sections")
        return description, requirements

    lines = [line for line in _plain_text(raw_requirements).splitlines() if line]
    requirement_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _heading_key(line) in REQUIREMENT_HEADINGS
        ),
        None,
    )
    if requirement_index is None:
        combined_body = "\n".join(lines).strip()
        if not combined_body:
            raise ValueError(f"Ctrip job {external_id} has an empty combined body")
        return combined_body, None
    responsibility_index = next(
        (
            index
            for index, line in enumerate(lines[:requirement_index])
            if _heading_key(line) in RESPONSIBILITY_HEADINGS
        ),
        None,
    )
    description_start = 0 if responsibility_index is None else responsibility_index + 1
    description = "\n".join(lines[description_start:requirement_index]).strip() or None
    requirements = "\n".join(lines[requirement_index + 1 :]).strip() or None
    if description is None and requirements is None:
        raise ValueError(f"Ctrip job {external_id} has empty explicit text sections")
    return description, requirements


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
    BLOCK_TAGS = {
        "br",
        "div",
        "li",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "tr",
        "ul",
    }

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
