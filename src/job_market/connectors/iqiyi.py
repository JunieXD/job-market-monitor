"""iQIYI public experienced and campus recruitment connector."""

import asyncio
import json
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import parse_qs, urlparse
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
MAX_COLLECTION_ATTEMPTS = 5
POSITION_ENDPOINT = "/api/v1/search/job/posts"
POSITION_LIST_URLS = {
    Channel.EXPERIENCED: "https://careers.iqiyi.com/job/",
    Channel.CAMPUS: "https://careers.iqiyi.com/campus/",
}
POSITION_DETAIL_PATHS = {
    Channel.EXPERIENCED: "job",
    Channel.CAMPUS: "campus",
}
EXPECTED_RECRUIT_PARENT_IDS = {
    Channel.EXPERIENCED: "1",
    Channel.CAMPUS: "2",
}


class IqiyiConnector:
    """Collect public lists while leaving request signatures to the site."""

    source_key = "iqiyi_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel not in POSITION_LIST_URLS:
            raise ValueError("iQIYI connector supports experienced and campus jobs")

        initial_payload = await self._open_root(channel)
        initial_total = self._position_page(
            initial_payload,
            expected_page=1,
        )["total"]
        jobs_by_id, declared_total, complete = await self._collect_root(
            channel,
            initial_payload,
            initial_total,
            max_pages,
        )
        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts={
                "all": declared_total,
                "root-initial": initial_total,
                "collected-unique": len(jobs_by_id),
            },
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_root(
        self,
        channel: Channel,
        initial_payload: dict[str, Any],
        initial_total: int,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool]:
        union_by_id: dict[str, JobRecord] = {}
        target_total = initial_total

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = initial_payload if attempt == 1 else await self._open_root(channel)
            first = self._position_page(payload, expected_page=1)
            if first["total"] != target_total:
                target_total = first["total"]
                union_by_id = {}
            total_pages = ceil(target_total / UI_PAGE_SIZE) if target_total else 0
            pass_by_id: dict[str, JobRecord] = {}
            attempt_label = "root" if attempt == 1 else f"root-retry-{attempt}"
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
                    record = self.parse_job(raw, channel)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"iQIYI job {record.external_id} changed within "
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
                            f"iQIYI job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        "iQIYI observations exceeded the declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            f"iQIYI list did not converge after {MAX_COLLECTION_ATTEMPTS} "
            f"attempts: declared={target_total}, union={len(union_by_id)}"
        )

    async def _open_root(self, channel: Channel) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = 1
        await self.page.goto(
            POSITION_LIST_URLS[channel],
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_payload(f"positions:{channel.value}:1")
        await self._assert_active_page(1)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_item = self.page.locator(".atsx-pagination-next")
        if await next_item.count() != 1:
            raise RuntimeError("iQIYI pagination has no unique next control")
        classes = await next_item.get_attribute("class") or ""
        if "atsx-pagination-disabled" in classes:
            raise RuntimeError(f"iQIYI pagination ended before page {page_number}")
        button = next_item.locator("button")
        await (button if await button.count() == 1 else next_item).click()
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".atsx-pagination-item-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"iQIYI page did not render active page {expected_page}")

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"iQIYI response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid iQIYI response for {operation}: {message}")
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
            raise RuntimeError("iQIYI response has no data object")
        rows = data.get("job_post_list")
        total = _response_int(data.get("count"), "count", allow_zero=True)
        if not isinstance(rows, list):
            raise RuntimeError("iQIYI response has no job list")
        expected_rows = min(
            UI_PAGE_SIZE,
            max(total - (expected_page - 1) * UI_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"iQIYI page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        if not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("iQIYI job list contains a non-object row")
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any], channel: Channel) -> JobRecord:
        if channel not in POSITION_DETAIL_PATHS:
            raise ValueError(f"Unsupported iQIYI channel: {channel.value}")
        external_id = _required(raw, "id", "iQIYI job id")
        title = _required(raw, "title", f"iQIYI job title ({external_id})")
        recruit_type = _required_object(
            raw.get("recruit_type"),
            f"iQIYI job recruit type ({external_id})",
        )
        employment_id = _required(
            recruit_type,
            "id",
            f"iQIYI job employment type id ({external_id})",
        )
        employment_name = _required(
            recruit_type,
            "name",
            f"iQIYI job employment type name ({external_id})",
        )
        parent = _required_object(
            recruit_type.get("parent"),
            f"iQIYI job recruit parent ({external_id})",
        )
        parent_id = _required(
            parent,
            "id",
            f"iQIYI job recruit parent id ({external_id})",
        )
        if parent_id != EXPECTED_RECRUIT_PARENT_IDS[channel]:
            raise ValueError(
                f"iQIYI job {external_id} recruit parent {parent_id!r} "
                f"does not match channel {channel.value}"
            )
        project_id, project_name = _project(raw.get("job_subject"), external_id)

        return JobRecord(
            source_key="iqiyi_cn",
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=(
                f"https://careers.iqiyi.com/{POSITION_DETAIL_PATHS[channel]}/"
                f"position/{external_id}/detail"
            ),
            company_name="爱奇艺",
            channel=channel,
            employment_type_id=employment_id,
            employment_type_name=employment_name,
            recruitment_project_id=project_id,
            recruitment_project_name=project_name,
            title=title,
            description=_optional(raw.get("description")),
            requirements=_optional(raw.get("requirement")),
            published_at=_timestamp_ms(raw.get("publish_time"), "publish_time"),
            is_hot=_optional_bool(raw.get("job_hot_flag"), external_id),
            locations=_locations(raw.get("city_list"), external_id),
            categories=_categories(raw.get("job_function"), external_id),
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200 or POSITION_ENDPOINT not in response.url:
            return
        params = parse_qs(urlparse(response.url).query, keep_blank_values=True)
        expected_offset = (self._active_page - 1) * UI_PAGE_SIZE
        if (
            _single_query_value(params, "limit") != str(UI_PAGE_SIZE)
            or _single_query_value(params, "offset") != str(expected_offset)
            or _single_query_value(params, "portal_type") != "6"
            or _single_query_value(params, "portal_entrance") != "1"
        ):
            return
        for key in (
            "keyword",
            "job_category_id_list",
            "tag_id_list",
            "location_code_list",
            "subject_id_list",
            "recruitment_id_list",
            "job_function_id_list",
            "storefront_id_list",
        ):
            if _single_query_value(params, key) != "":
                return
        if not _single_query_value(params, "_signature"):
            return
        enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.iqiyi_request_delay_seconds - (
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


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing")
    return value


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"iQIYI {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"iQIYI {field} is invalid: {value!r}")
    return value


def _single_query_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values is not None and len(values) == 1 else None


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"iQIYI {field} is not a millisecond timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))


def _optional_bool(value: Any, external_id: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"iQIYI job {external_id} hot flag is invalid: {value!r}")
    return value


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"iQIYI job {external_id} city list is invalid")
    locations: list[LocationRecord] = []
    for raw in value:
        item = _required_object(raw, f"iQIYI job city ({external_id})")
        locations.append(
            LocationRecord(
                code=_required(item, "code", f"iQIYI job city code ({external_id})"),
                name=_required(item, "name", f"iQIYI job city name ({external_id})"),
            )
        )
    return locations


def _categories(value: Any, external_id: str) -> list[SourceCategoryRecord]:
    if value is None:
        return []
    item = _required_object(value, f"iQIYI job function ({external_id})")
    parent = item.get("parent")
    parent_id: str | None = None
    parent_name: str | None = None
    if parent is not None:
        parent_item = _required_object(parent, f"iQIYI job function parent ({external_id})")
        parent_id = _required(
            parent_item,
            "id",
            f"iQIYI job function parent id ({external_id})",
        )
        parent_name = _required(
            parent_item,
            "name",
            f"iQIYI job function parent name ({external_id})",
        )
    return [
        SourceCategoryRecord(
            external_id=_required(
                item,
                "id",
                f"iQIYI job function id ({external_id})",
            ),
            name=_required(
                item,
                "name",
                f"iQIYI job function name ({external_id})",
            ),
            parent_external_id=parent_id,
            parent_name=parent_name,
            assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
        )
    ]


def _project(value: Any, external_id: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    item = _required_object(value, f"iQIYI job subject ({external_id})")
    name = item.get("name")
    if isinstance(name, dict):
        project_name = _optional(name.get("i18n")) or _optional(name.get("zh_cn"))
    else:
        project_name = _optional(name)
    project_id = _optional(item.get("id"))
    if (project_id is None) != (project_name is None):
        raise ValueError(f"iQIYI job {external_id} has an incomplete subject")
    return project_id, project_name
