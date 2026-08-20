"""Lilith Games public experienced, campus, and internship connector."""

import asyncio
import json
from dataclasses import dataclass
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
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

API_CAP = 10_000
PAGE_SIZE = 200
MAX_STABILITY_PASSES = 3
RESPONSE_TIMEOUT_SECONDS = 30
POSITION_ENDPOINT = "/api/v1/search/job/posts"


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    website_path: str
    expected_recruit_parent_id: str

    def list_url(self, page_number: int) -> str:
        return (
            f"https://lilithgames.jobs.feishu.cn/{self.website_path}"
            f"?current={page_number}&limit={PAGE_SIZE}"
        )

    def detail_url(self, external_id: str) -> str:
        return (
            f"https://lilithgames.jobs.feishu.cn/{self.website_path}/"
            f"position/{external_id}/detail"
        )


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(Channel.EXPERIENCED, "career", "1"),
    Channel.CAMPUS: PortalConfig(Channel.CAMPUS, "campus", "2"),
    Channel.INTERNSHIP: PortalConfig(Channel.INTERNSHIP, "intern", "2"),
}


class LilithConnector:
    """Read only successful requests signed and sent by the official pages."""

    source_key = "lilith_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_portal: PortalConfig | None = None
        self._active_page_number = 1
        self._collection_channel: Channel | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Lilith connector does not support this channel")
        self._collection_channel = channel

        rows, total, complete, issues = await self._collect_stable(
            portal,
            partition="portal",
            max_pages=max_pages,
        )
        partition_counts = {
            "portal-all": total,
            "portal-unique": len(rows),
        }
        excluded_ids: set[str] = set()

        # The campus page currently repeats one job from the explicit internship
        # page. Keep source IDs direct and stable by assigning shared membership
        # to the more specific internship channel.
        if channel is Channel.CAMPUS:
            internship_rows, internship_total, membership_complete, membership_issues = (
                await self._collect_stable(
                    PORTALS[Channel.INTERNSHIP],
                    partition="intern-membership",
                    max_pages=max_pages,
                )
            )
            excluded_ids = set(rows).intersection(internship_rows)
            partition_counts["intern-membership"] = internship_total
            partition_counts["excluded-intern-overlap"] = len(excluded_ids)
            complete = complete and membership_complete
            issues.extend(membership_issues)

        selected_rows = [
            row
            for external_id, row in rows.items()
            if external_id not in excluded_ids
        ]
        jobs = [self.parse_job(row, portal) for row in selected_rows]
        partition_counts["all"] = len(jobs)
        partition_counts["collected-unique"] = len(jobs)
        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
            issues=issues,
        )

    async def _collect_stable(
        self,
        portal: PortalConfig,
        *,
        partition: str,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], int, bool, list[CollectionIssue]]:
        rows, total, complete = await self._collect_pass(
            portal,
            partition=f"{partition}-initial",
            max_pages=max_pages,
        )
        if not complete or total <= PAGE_SIZE:
            return rows, total, complete, []

        previous_rows = rows
        previous_total = total
        for attempt in range(2, MAX_STABILITY_PASSES + 1):
            current_rows, current_total, current_complete = await self._collect_pass(
                portal,
                partition=f"{partition}-stability-{attempt}",
                max_pages=max_pages,
            )
            if not current_complete:
                effective_total = current_total if current_rows else previous_total
                return current_rows or previous_rows, effective_total, False, []
            if _same_observation(
                previous_rows,
                previous_total,
                current_rows,
                current_total,
            ):
                return current_rows, current_total, True, []
            previous_rows = current_rows
            previous_total = current_total

        issue = CollectionIssue(
            scope="source",
            error_type="ListChangedDuringPagination",
            message=(
                f"Lilith {portal.website_path} list did not produce two consecutive "
                f"stable passes after {MAX_STABILITY_PASSES} attempts"
            ),
        )
        return previous_rows, previous_total, False, [issue]

    async def _collect_pass(
        self,
        portal: PortalConfig,
        *,
        partition: str,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], int, bool]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        total: int | None = None
        total_pages = 1
        page_number = 1
        while page_number <= total_pages:
            if max_pages is not None and self.pages_fetched >= max_pages:
                return rows_by_id, total or 0, False
            payload = await self._open_page(portal, page_number)
            current = self._position_page(payload, expected_page=page_number)
            if total is None:
                total = current["total"]
                total_pages = max(1, ceil(total / PAGE_SIZE))
            elif current["total"] != total:
                raise RuntimeError(
                    "Lilith total changed within a page pass: "
                    f"expected={total}, got={current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(
                self._collection_channel or portal.channel,
                partition,
                (page_number - 1) * PAGE_SIZE,
                payload,
            )
            for raw in current["rows"]:
                external_id = _required(raw, "id", "Lilith job id")
                if external_id in rows_by_id:
                    raise RuntimeError(f"Lilith page pass repeated job {external_id}")
                rows_by_id[external_id] = raw
            page_number += 1

        if total is None:
            raise AssertionError("Lilith page pass returned no pagination metadata")
        if len(rows_by_id) != total:
            raise RuntimeError(
                f"Lilith pagination count mismatch: declared={total}, "
                f"unique={len(rows_by_id)}"
            )
        return rows_by_id, total, True

    async def _open_page(
        self,
        portal: PortalConfig,
        page_number: int,
    ) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_portal = portal
        self._active_page_number = page_number
        await self.page.goto(
            portal.list_url(page_number),
            wait_until="commit",
            timeout=60_000,
        )
        payload = await self._next_payload(
            f"{portal.website_path}:{page_number}"
        )
        await self.page.evaluate("window.stop()")
        return payload

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Lilith response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Lilith response for {operation}: {message}")
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
            raise RuntimeError("Lilith response has no data object")
        rows = data.get("job_post_list")
        total = _response_int(data.get("count"), "count", allow_zero=True)
        if total >= API_CAP:
            raise RuntimeError(
                f"Lilith response reached the {API_CAP} job API cap; "
                "a lossless partition strategy is required"
            )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Lilith response has an invalid job list")
        expected_rows = min(
            PAGE_SIZE,
            max(total - (expected_page - 1) * PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Lilith page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any], portal: PortalConfig) -> JobRecord:
        external_id = _required(raw, "id", "Lilith job id")
        title = _required(raw, "title", f"Lilith job title ({external_id})")
        recruit_type = _required_object(
            raw.get("recruit_type"),
            f"Lilith recruit type ({external_id})",
        )
        employment_id = _required(
            recruit_type,
            "id",
            f"Lilith employment type id ({external_id})",
        )
        employment_name = _required(
            recruit_type,
            "name",
            f"Lilith employment type name ({external_id})",
        )
        parent = _required_object(
            recruit_type.get("parent"),
            f"Lilith recruit type parent ({external_id})",
        )
        parent_id = _required(
            parent,
            "id",
            f"Lilith recruit type parent id ({external_id})",
        )
        if parent_id != portal.expected_recruit_parent_id:
            raise ValueError(
                f"Lilith job {external_id} recruit parent {parent_id!r} does not "
                f"match channel {portal.channel.value}"
            )
        project_id, project_name = _project(raw.get("job_subject"), external_id)
        info = raw.get("job_post_info")
        source_status = None
        if isinstance(info, dict):
            source_status = _optional(info.get("job_active_status"))

        return JobRecord(
            source_key="lilith_cn",
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=portal.detail_url(external_id),
            company_name="莉莉丝游戏",
            channel=portal.channel,
            employment_type_id=employment_id,
            employment_type_name=employment_name,
            recruitment_project_id=project_id,
            recruitment_project_name=project_name,
            title=title,
            description=_optional(raw.get("description")),
            requirements=_optional(raw.get("requirement")),
            published_at=_timestamp_ms(raw.get("publish_time"), "publish_time"),
            source_status=source_status,
            department_code=_optional(raw.get("department_id")),
            is_hot=_optional_bool(raw.get("job_hot_flag"), external_id),
            locations=_locations(raw.get("city_list"), info, external_id),
            categories=_categories(raw.get("job_function"), external_id),
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        portal = self._active_portal
        if (
            portal is None
            or response.status != 200
            or POSITION_ENDPOINT not in response.url
            or response.request.method != "POST"
        ):
            return
        headers = response.request.headers
        if headers.get("website-path") != portal.website_path:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        expected_offset = (self._active_page_number - 1) * PAGE_SIZE
        expected = {
            "keyword": "",
            "limit": PAGE_SIZE,
            "offset": expected_offset,
            "job_category_id_list": [],
            "tag_id_list": [],
            "location_code_list": [],
            "subject_id_list": [],
            "recruitment_id_list": [],
            "portal_type": 6,
            "job_function_id_list": [],
            "storefront_id_list": [],
            "portal_entrance": 1,
        }
        params = parse_qs(urlparse(response.url).query, keep_blank_values=True)
        if post_data != expected or not _single_query_value(params, "_signature"):
            return
        for key, value in (
            ("limit", str(PAGE_SIZE)),
            ("offset", str(expected_offset)),
            ("portal_type", "6"),
            ("portal_entrance", "1"),
        ):
            if _single_query_value(params, key) != value:
                return
        enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.lilith_request_delay_seconds - (
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


def _same_observation(
    left: dict[str, dict[str, Any]],
    left_total: int,
    right: dict[str, dict[str, Any]],
    right_total: int,
) -> bool:
    if left_total != right_total or set(left) != set(right):
        return False
    return all(
        json.dumps(left[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == json.dumps(right[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key in left
    )


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{label} is missing")
    result = _optional(value)
    if result is None:
        raise ValueError(f"{label} is missing")
    return result


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing")
    return value


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Lilith {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Lilith {field} is invalid: {value!r}")
    return value


def _single_query_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values is not None and len(values) == 1 else None


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Lilith {field} is not a millisecond timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))


def _optional_bool(value: Any, external_id: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Lilith job {external_id} hot flag is invalid")
    return value


def _locations(
    city_value: Any,
    info_value: Any,
    external_id: str,
) -> list[LocationRecord]:
    if city_value is None:
        cities: list[Any] = []
    elif isinstance(city_value, list):
        cities = city_value
    else:
        raise ValueError(f"Lilith job {external_id} city list is invalid")
    info = info_value if isinstance(info_value, dict) else {}
    addresses = info.get("address_list") or []
    if not isinstance(addresses, list):
        raise ValueError(f"Lilith job {external_id} address list is invalid")
    address_by_city: dict[str, dict[str, Any]] = {}
    for raw in addresses:
        address = _required_object(raw, f"Lilith address ({external_id})")
        city = address.get("city") or {}
        if isinstance(city, dict) and _optional(city.get("city_code")) is not None:
            address_by_city[str(city["city_code"])] = address
    if not cities:
        cities = [
            address.get("city")
            for address in addresses
            if isinstance(address, dict) and isinstance(address.get("city"), dict)
        ]

    result: list[LocationRecord] = []
    seen: set[str] = set()
    for raw in cities:
        city = _required_object(raw, f"Lilith city ({external_id})")
        code = _optional(city.get("code")) or _optional(city.get("city_code"))
        name = _optional(city.get("name"))
        if code is None or name is None:
            raise ValueError(f"Lilith job {external_id} has an incomplete city")
        if code in seen:
            continue
        seen.add(code)
        address = address_by_city.get(code) or {}
        country = address.get("country") or {}
        state = address.get("state") or {}
        district = address.get("district") or {}
        result.append(
            LocationRecord(
                code=code,
                name=name,
                country_code=_optional(country.get("country_code")),
                country_name=_optional(country.get("name")),
                state_code=_optional(state.get("state_code")),
                state_name=_optional(state.get("name")),
                district_code=_optional(district.get("district_code")),
                district_name=_optional(district.get("name")),
                address=_optional(address.get("name")),
            )
        )
    return result


def _categories(value: Any, external_id: str) -> list[SourceCategoryRecord]:
    if value is None:
        return []
    item = _required_object(value, f"Lilith job function ({external_id})")
    parent = item.get("parent")
    parent_id: str | None = None
    parent_name: str | None = None
    if parent is not None:
        parent_item = _required_object(parent, f"Lilith job function parent ({external_id})")
        parent_id = _required(
            parent_item,
            "id",
            f"Lilith job function parent id ({external_id})",
        )
        parent_name = _required(
            parent_item,
            "name",
            f"Lilith job function parent name ({external_id})",
        )
    return [
        SourceCategoryRecord(
            external_id=_required(
                item,
                "id",
                f"Lilith job function id ({external_id})",
            ),
            name=_required(
                item,
                "name",
                f"Lilith job function name ({external_id})",
            ),
            parent_external_id=parent_id,
            parent_name=parent_name,
            assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
        )
    ]


def _project(value: Any, external_id: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    item = _required_object(value, f"Lilith job subject ({external_id})")
    name = item.get("name")
    if isinstance(name, dict):
        project_name = _optional(name.get("i18n")) or _optional(name.get("zh_cn"))
    else:
        project_name = _optional(name)
    project_id = _optional(item.get("id"))
    if (project_id is None) != (project_name is None):
        raise ValueError(f"Lilith job {external_id} has an incomplete subject")
    return project_id, project_name
