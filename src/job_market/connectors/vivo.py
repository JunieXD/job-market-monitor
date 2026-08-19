"""vivo public experienced-recruitment connector."""

import asyncio
import json
from datetime import datetime
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
POSITION_LIST_URL = "https://hr.vivo.com/jobs"
POSITION_ENDPOINT = "/api/social/webSite/portal/page"
CATEGORY_ENDPOINT = "/api/social/webSite/portal/jobCategory"


class VivoConnector:
    """Collect vivo's social list and converge across pagination drift."""

    source_key = "vivo_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self._category_catalog: dict[str, tuple[str, str | None, str | None]] | None = (
            None
        )
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("vivo connector supports only the experienced channel")

        await self.page.route("**/*", _skip_nonessential_assets)
        root_payload = await self._open_root()
        root = self._position_page(root_payload, expected_page=1)
        total_count = root["total"]
        jobs_by_id, complete = await self._collect_root(
            channel,
            root_payload,
            total_count,
            max_pages,
        )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts={"all": total_count},
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_root(
        self,
        channel: Channel,
        initial_payload: dict[str, Any],
        total_count: int,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], bool]:
        union_by_id: dict[str, JobRecord] = {}
        total_pages = ceil(total_count / UI_PAGE_SIZE) if total_count else 0

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = initial_payload if attempt == 1 else await self._open_root()
            first = self._position_page(payload, expected_page=1)
            if first["total"] != total_count:
                raise RuntimeError(
                    "vivo root total changed during collection: "
                    f"initial={total_count}, current={first['total']}"
                )

            attempt_label = "root" if attempt == 1 else f"root-retry-{attempt}"
            pass_by_id: dict[str, JobRecord] = {}
            if total_pages == 0:
                self._save_payload(channel, attempt_label, 0, payload)

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, False
                await self._assert_active_page(page_number)
                current = self._position_page(payload, expected_page=page_number)
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    attempt_label,
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw, self._require_category_catalog())
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"vivo job {record.external_id} changed within "
                            f"{attempt_label}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._next_page(page_number + 1)

            for external_id, record in pass_by_id.items():
                previous = union_by_id.get(external_id)
                if previous is not None and (
                    previous.content_hash() != record.content_hash()
                ):
                    raise RuntimeError(
                        f"vivo job {external_id} changed during root retries"
                    )
                union_by_id[external_id] = record
            if len(union_by_id) == total_count:
                return union_by_id, True
            if len(union_by_id) > total_count:
                raise RuntimeError(
                    "vivo root observations exceeded the stable declared total: "
                    f"declared={total_count}, unique={len(union_by_id)}"
                )

        raise RuntimeError(
            f"vivo root list did not converge after {MAX_COLLECTION_ATTEMPTS} "
            f"attempts: declared={total_count}, union={len(union_by_id)}"
        )

    async def _open_root(self) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        if self._category_catalog is None:
            drain_json_responses(self._category_responses)
        self._active_page = 1
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_success(self._position_responses, "positions:1")
        if self._category_catalog is None:
            category_payload = await self._next_success(
                self._category_responses,
                "category catalog",
            )
            self._category_catalog = self.parse_category_catalog(category_payload)
            self._save_payload(
                Channel.EXPERIENCED,
                "category-catalog",
                0,
                category_payload,
            )
        await self._assert_active_page(1)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_button = self.page.locator(".el-pagination .btn-next")
        if await next_button.count() != 1:
            raise RuntimeError("vivo pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(f"vivo pagination ended before page {page_number}")
        await next_button.click()
        payload = await self._next_success(
            self._position_responses,
            f"positions:{page_number}",
        )
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".el-pagination .number.active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"vivo page did not render active page {expected_page}")

    async def _next_success(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"vivo response: {operation}",
        )
        if payload.get("code") != 0 or payload.get("success") is not True:
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid vivo response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        rows = payload.get("data")
        meta = payload.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise RuntimeError("vivo position response has invalid data or metadata")
        total = _response_int(meta.get("total"), "total", allow_zero=True)
        page_number = _response_int(meta.get("page"), "page")
        page_size = _response_int(meta.get("max_results"), "max_results")
        page_count = _response_int(
            meta.get("page_count"),
            "page_count",
            allow_zero=True,
        )
        if page_number != expected_page:
            raise RuntimeError(
                f"vivo page mismatch: expected={expected_page}, got={page_number}"
            )
        if page_size != UI_PAGE_SIZE:
            raise RuntimeError(
                f"vivo page size changed: expected={UI_PAGE_SIZE}, got={page_size}"
            )
        expected_pages = ceil(total / page_size) if total else 0
        if page_count != expected_pages:
            raise RuntimeError(
                f"vivo page count mismatch: expected={expected_pages}, got={page_count}"
            )
        expected_rows = min(
            page_size,
            max(total - (page_number - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"vivo page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"vivo page {page_number} has a non-object row")
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_category_catalog(
        payload: dict[str, Any],
    ) -> dict[str, tuple[str, str | None, str | None]]:
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("vivo category catalog is empty")
        basic: dict[str, tuple[str, str]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                raise RuntimeError("vivo category catalog has a non-object row")
            external_id = _required(raw, "id", "vivo category id")
            name = _required(raw, "name", f"vivo category name ({external_id})")
            parent_id = _required(
                raw,
                "parent_id",
                f"vivo category parent ({external_id})",
            )
            if external_id in basic:
                raise RuntimeError(f"vivo category catalog repeats {external_id!r}")
            basic[external_id] = (name, parent_id)

        catalog: dict[str, tuple[str, str | None, str | None]] = {}
        for external_id, (name, parent_id) in basic.items():
            parent = basic.get(parent_id)
            catalog[external_id] = (
                name,
                parent_id if parent is not None else None,
                parent[0] if parent is not None else None,
            )
        return catalog

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        categories: dict[str, tuple[str, str | None, str | None]],
    ) -> JobRecord:
        external_id = _required(raw, "job_id", "vivo job id")
        category_id = _required(
            raw,
            "job_category_id",
            f"vivo job category ({external_id})",
        )
        category = categories.get(category_id)
        if category is None:
            raise ValueError(
                f"vivo job {external_id} has unknown category {category_id!r}"
            )
        category_name, parent_id, parent_name = category
        direct_category_name = _required(
            raw,
            "job_category",
            f"vivo job category name ({external_id})",
        )
        if direct_category_name != category_name:
            raise ValueError(
                f"vivo job {external_id} category name disagrees with the catalog"
            )
        degree_code = _optional(raw.get("degree_range_code"))
        degree_name = _optional(raw.get("degree_range_name"))
        if (degree_code is None) != (degree_name is None):
            degree_code = None
            degree_name = None
        experience_min = _source_bound(raw.get("yoe_min"), "yoe_min", external_id)
        experience_max = _source_bound(raw.get("yoe_max"), "yoe_max", external_id)
        head_count = _non_negative_int(raw.get("head_count"), "head_count", external_id)
        fuzzy_count = _non_negative_int(
            raw.get("fuzzy_head_count"),
            "fuzzy_head_count",
            external_id,
        )
        hot = raw.get("hot")
        if not isinstance(hot, bool):
            raise ValueError(f"vivo job {external_id} hot is not boolean")

        return JobRecord(
            source_key="vivo_social_cn",
            external_id=external_id,
            external_code=_optional(raw.get("job_code")),
            source_url=POSITION_LIST_URL,
            company_name="vivo",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=_required(raw, "job_title", f"vivo job title ({external_id})"),
            description=_optional(raw.get("job_desc")),
            requirements=None,
            published_at=_timestamp_ms(raw.get("publish_timestamp"), "publish_timestamp"),
            recruitment_count=head_count if fuzzy_count == 0 else None,
            degree_code=degree_code,
            degree_name=degree_name,
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            department_name=_optional(raw.get("requirement_org_name")),
            is_hot=hot,
            locations=_locations(raw.get("job_location_list"), external_id),
            categories=[
                SourceCategoryRecord(
                    external_id=category_id,
                    name=category_name,
                    parent_external_id=parent_id,
                    parent_name=parent_name,
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
            expected = {
                "city_code_list": [],
                "company_id": 1,
                "group_id": 1,
                "user_id": None,
                "job_category_id_list": [],
                "keyword": "",
                "max_results": UI_PAGE_SIZE,
                "page": self._active_page,
                "yoe_list": [],
                "loading": True,
            }
            if post_data == expected:
                enqueue_json_response(self._position_responses, response)
            return
        if CATEGORY_ENDPOINT in response.url and self._category_catalog is None:
            enqueue_json_response(self._category_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.vivo_request_delay_seconds - (
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

    def _require_category_catalog(
        self,
    ) -> dict[str, tuple[str, str | None, str | None]]:
        if self._category_catalog is None:
            raise RuntimeError("vivo category catalog has not been loaded")
        return self._category_catalog


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
        raise RuntimeError(f"vivo {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"vivo {field} is invalid: {value!r}")
    return value


def _non_negative_int(value: Any, field: str, external_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"vivo job {external_id} {field} is invalid: {value!r}")
    return value


def _source_bound(value: Any, field: str, external_id: str) -> int | None:
    if value == -1:
        return None
    return _non_negative_int(value, field, external_id)


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"vivo {field} is not a millisecond timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"vivo job {external_id} has no location list")
    grouped: dict[str, tuple[str, set[str]]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(f"vivo job {external_id} has a non-object location")
        code = _required(raw, "city_code", f"vivo job city code ({external_id})")
        name = _required(raw, "city", f"vivo job city name ({external_id})")
        address = _optional(raw.get("location"))
        previous = grouped.get(code)
        if previous is None:
            grouped[code] = (name, {address} if address else set())
        else:
            if previous[0] != name:
                raise ValueError(
                    f"vivo job {external_id} has conflicting city name for {code}"
                )
            if address:
                previous[1].add(address)
    return [
        LocationRecord(
            code=code,
            name=name,
            address=next(iter(addresses)) if len(addresses) == 1 else None,
        )
        for code, (name, addresses) in grouped.items()
    ]
