"""Ant Group public experienced-recruitment connector."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urljoin

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
COLLECTION_PAGE_SIZE = 49
MAX_COLLECTION_ATTEMPTS = 5
POSITION_LIST_URL = "https://talent.antgroup.com/off-campus"


@dataclass(frozen=True)
class CategoryFilter:
    code: str
    name: str


class AntConnector:
    """Collect complete social-recruitment responses through Ant's public UI."""

    source_key = "ant_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._active_category_code = ""
        self._category_catalog: tuple[CategoryFilter, ...] | None = None
        self._position_api_url: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Ant connector supports only the experienced channel")

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
        total_pages = ceil(total_count / COLLECTION_PAGE_SIZE) if total_count else 0

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = initial_payload if attempt == 1 else await self._open_root()
            first = self._position_page(payload, expected_page=1)
            if first["total"] != total_count:
                raise RuntimeError(
                    "Ant root total changed during collection: "
                    f"initial={total_count}, current={first['total']}"
                )

            attempt_partition = "root" if attempt == 1 else f"root-retry-{attempt}"
            pass_by_id: dict[str, JobRecord] = {}
            if total_pages == 0:
                self._save_payload(channel, attempt_partition, 1, payload)

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, False
                current = self._position_page(payload, expected_page=page_number)
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    attempt_partition,
                    page_number,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Ant returned conflicting job {record.external_id} "
                            f"within {attempt_partition}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._fetch_page(page_number + 1)

            for external_id, record in pass_by_id.items():
                previous = union_by_id.get(external_id)
                if previous is not None and (
                    previous.content_hash() != record.content_hash()
                ):
                    raise RuntimeError(
                        f"Ant changed job {external_id} during root retries"
                    )
                union_by_id[external_id] = record
            if len(union_by_id) == total_count:
                return union_by_id, True
            if len(union_by_id) > total_count:
                raise RuntimeError(
                    "Ant root observations exceeded the stable declared total: "
                    f"declared={total_count}, unique={len(union_by_id)}"
                )

        raise RuntimeError(
            f"Ant root list did not converge after {MAX_COLLECTION_ATTEMPTS} attempts: "
            f"declared={total_count}, union={len(union_by_id)}"
        )

    async def _open_root(self) -> dict[str, Any]:
        if self._position_api_url is not None:
            return await self._fetch_page(1)
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        if self._category_catalog is None:
            drain_json_responses(self._category_responses)
        self._active_category_code = ""
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await self._next_payload("positions:root:1")
        if self._category_catalog is None:
            category_payload = await next_json_payload(
                self._category_responses,
                timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
                operation="Ant category catalog",
            )
            self._category_catalog = self.parse_category_catalog(category_payload)
            self._save_category_catalog(category_payload)
        if self._position_api_url is None:
            raise RuntimeError("Ant position API URL was not observed")
        # Unload the large recruitment SPA after it supplied the public token
        # and category catalog. All position pages use the official JSON API.
        await self.page.goto(
            "about:blank",
            wait_until="commit",
            timeout=60_000,
        )
        return await self._fetch_page(1)

    async def _fetch_page(self, page_number: int) -> dict[str, Any]:
        if self._position_api_url is None:
            raise RuntimeError("Ant position API URL is unavailable")
        await self._rate_limit()
        response = await self.page.request.post(
            self._position_api_url,
            headers={"Content-Type": "application/json"},
            data={
                "key": "",
                "regions": "",
                "categories": "",
                "subCategories": "",
                "bgCode": "",
                "socialQrCode": "",
                "pageIndex": page_number,
                "pageSize": COLLECTION_PAGE_SIZE,
                "channel": "group_official_site",
                "language": "zh",
            },
            timeout=60_000,
            fail_on_status_code=False,
        )
        if response.status != 200:
            raise RuntimeError(
                f"Ant page {page_number} returned HTTP {response.status}"
            )
        payload = await response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Ant page {page_number} returned invalid JSON")
        if payload.get("success") is not True or not isinstance(
            payload.get("content"), list
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Ant response for page {page_number}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Ant response: {operation}",
        )
        if payload.get("success") is not True or not isinstance(
            payload.get("content"), list
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Ant response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        rows = payload.get("content")
        if not isinstance(rows, list):
            raise RuntimeError("Ant response has no job list")
        total = _response_int(payload.get("totalCount"), "totalCount", allow_zero=True)
        page_size = _response_int(payload.get("pageSize"), "pageSize")
        page_number = _response_int(payload.get("currentPage"), "currentPage")
        if page_size != COLLECTION_PAGE_SIZE:
            raise RuntimeError(
                "Ant page size changed: "
                f"expected={COLLECTION_PAGE_SIZE}, got={page_size}"
            )
        if page_number != expected_page:
            raise RuntimeError(
                f"Ant page mismatch: expected={expected_page}, got={page_number}"
            )
        expected_rows = min(
            page_size,
            max(total - (page_number - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Ant page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"Ant page {page_number} contains a non-object row")
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_category_catalog(payload: dict[str, Any]) -> tuple[CategoryFilter, ...]:
        if payload.get("success") is not True or not isinstance(
            payload.get("content"), list
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Ant category catalog: {message}")
        categories: list[CategoryFilter] = []
        seen_codes: set[str] = set()
        seen_names: set[str] = set()
        for raw in payload["content"]:
            if not isinstance(raw, dict):
                raise RuntimeError("Ant category catalog contains a non-object root")
            code = _required(raw, "code", "Ant category code")
            name = _required(raw, "name", f"Ant category name ({code})")
            if code in seen_codes or name in seen_names:
                raise RuntimeError(
                    f"Ant category catalog has a duplicate root: {code!r}/{name!r}"
                )
            seen_codes.add(code)
            seen_names.add(name)
            categories.append(CategoryFilter(code=code, name=name))
        if not categories:
            raise RuntimeError("Ant category catalog is empty")
        return tuple(categories)

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "id", "Ant job id")
        title = _required(raw, "name", f"Ant job title ({external_id})")
        description = _required(
            raw,
            "description",
            f"Ant job description ({external_id})",
        )
        requirements = _required(
            raw,
            "requirement",
            f"Ant job requirements ({external_id})",
        )
        locations = _string_list(raw.get("workLocations"), "workLocations", external_id)
        if not locations:
            raise ValueError(f"Ant job {external_id} has no work location")
        categories = _string_list(raw.get("categories"), "categories", external_id)
        interview_locations = _optional_string_list(
            raw.get("interviewLocations"),
            "interviewLocations",
            external_id,
        )
        experience_min, experience_max = _experience_range(
            raw.get("experience"),
            external_id,
        )
        department_name = _optional(raw.get("department"))
        department_path = _optional(raw.get("departmentPath"))
        source_url = _optional(raw.get("positionUrl"))
        if source_url is None:
            source_url = POSITION_LIST_URL
        else:
            source_url = urljoin(POSITION_LIST_URL, source_url)

        return JobRecord(
            source_key="ant_cn",
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=source_url,
            company_name="蚂蚁集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            recruitment_project_id=_optional(raw.get("batchId")),
            recruitment_project_name=_optional(raw.get("batchName")),
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_datetime(raw.get("publishTime"), "publishTime"),
            degree_code=_optional(raw.get("degree")),
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            department_code=department_path,
            department_name=department_name,
            interview_location_names=interview_locations,
            locations=[
                LocationRecord(code=f"name:{name}", name=name) for name in locations
            ],
            categories=[
                SourceCategoryRecord(
                    external_id=f"label:{name}",
                    name=name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
                for name in categories
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if (
            response.status == 200
            and "/api/social/category/list" in response.url
            and "/listDept" not in response.url
            and self._category_catalog is None
        ):
            enqueue_json_response(self._category_responses, response)
            return
        if (
            response.status == 200
            and "/api/social/position/search" in response.url
        ):
            try:
                post_data = response.request.post_data_json
            except Exception:
                return
            if not isinstance(post_data, dict):
                return
            self._position_api_url = response.url
            if post_data.get("pageSize") != 10:
                return
            if _optional(post_data.get("categories")) != (
                self._active_category_code or None
            ):
                return
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.ant_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _save_payload(
        self,
        channel: Channel,
        partition: str,
        page_number: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition=partition,
                    offset=page_number - 1,
                    payload=payload,
                )
            )

    def _save_category_catalog(self, payload: dict[str, Any]) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=Channel.EXPERIENCED,
                    partition="category-catalog",
                    offset=0,
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
        raise RuntimeError(f"Ant {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Ant {field} is not an integer: {value!r}")
    return value


def _source_datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Ant {field} is not an ISO datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Ant {field} has no source timezone: {text!r}")
    return parsed


def _string_list(value: Any, field: str, external_id: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Ant job {external_id} {field} is not a list")
    items: list[str] = []
    for raw_item in value:
        item = _optional(raw_item)
        if item is None:
            raise ValueError(f"Ant job {external_id} {field} contains an empty item")
        items.append(item)
    if len(set(items)) != len(items):
        raise ValueError(f"Ant job {external_id} {field} contains duplicates")
    return items


def _optional_string_list(value: Any, field: str, external_id: str) -> list[str]:
    if value is None:
        return []
    return _string_list(value, field, external_id)


def _experience_range(value: Any, external_id: str) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"Ant job {external_id} experience is not an object")
    minimum = _optional_non_negative_int(value.get("from"), "experience.from", external_id)
    maximum = _optional_non_negative_int(value.get("to"), "experience.to", external_id)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"Ant job {external_id} has an inverted experience range")
    return minimum, maximum


def _optional_non_negative_int(
    value: Any,
    field: str,
    external_id: str,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Ant job {external_id} {field} is invalid: {value!r}")
    return value
