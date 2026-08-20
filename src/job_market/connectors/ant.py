"""Ant Group public experienced-recruitment connector."""

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Page

from job_market.config import Settings
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

COLLECTION_PAGE_SIZE = 49
MAX_COLLECTION_ATTEMPTS = 5
POSITION_LIST_URL = "https://talent.antgroup.com/off-campus"
BOOTSTRAP_URL = "https://talent.antgroup.com/robots.txt"
API_BASE_URL = "https://hrcareersweb.antgroup.com/api/social"
CATEGORY_API_URL = f"{API_BASE_URL}/category/list"
POSITION_API_URL = f"{API_BASE_URL}/position/search"
FETCH_JSON_SCRIPT = """
async ({url, token, data}) => {
    const response = await fetch(
        `${url}?ctoken=${encodeURIComponent(token)}`,
        {
            method: "POST",
            credentials: "include",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(data),
        },
    );
    let payload = null;
    try {
        payload = await response.json();
    } catch (_) {
        // The caller reports a useful status/error when the endpoint is not JSON.
    }
    return {status: response.status, payload};
}
"""


@dataclass(frozen=True)
class CategoryFilter:
    code: str
    name: str


class AntConnector:
    """Collect complete social-recruitment responses from Ant's public API."""

    source_key = "ant_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._category_catalog: tuple[CategoryFilter, ...] | None = None
        self._csrf_token = f"bigfish_ctoken_{secrets.token_hex(12)}"
        self._session_initialized = False

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
        last_unstable_job_id: str | None = None

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
            unstable_job_id: str | None = None
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
                        unstable_job_id = record.external_id
                        break
                    pass_by_id[record.external_id] = record
                if unstable_job_id is not None:
                    break
                if page_number < total_pages:
                    payload = await self._fetch_page(page_number + 1)

            if unstable_job_id is not None:
                last_unstable_job_id = unstable_job_id
                union_by_id.clear()
                continue

            for external_id, record in pass_by_id.items():
                previous = union_by_id.get(external_id)
                if previous is not None and (
                    previous.content_hash() != record.content_hash()
                ):
                    unstable_job_id = external_id
                    break
                union_by_id[external_id] = record
            if unstable_job_id is not None:
                last_unstable_job_id = unstable_job_id
                union_by_id.clear()
                continue
            if len(union_by_id) == total_count:
                return union_by_id, True
            if len(union_by_id) > total_count:
                raise RuntimeError(
                    "Ant root observations exceeded the stable declared total: "
                    f"declared={total_count}, unique={len(union_by_id)}"
                )

        raise RuntimeError(
            f"Ant root list did not converge after {MAX_COLLECTION_ATTEMPTS} attempts: "
            f"declared={total_count}, union={len(union_by_id)}, "
            f"last_unstable_job={last_unstable_job_id}"
        )

    async def _open_root(self) -> dict[str, Any]:
        if not self._session_initialized:
            await self.page.context.add_cookies(
                [
                    {
                        "name": "ctoken",
                        "value": self._csrf_token,
                        "domain": ".antgroup.com",
                        "path": "/",
                    }
                ]
            )
            await self.page.goto(
                BOOTSTRAP_URL,
                wait_until="commit",
                timeout=60_000,
            )
            await self.page.evaluate("window.stop()")
            self._session_initialized = True
        if self._category_catalog is None:
            category_payload = await self._post_json(
                CATEGORY_API_URL,
                data={},
                operation="category catalog",
            )
            self._category_catalog = self.parse_category_catalog(category_payload)
            self._save_category_catalog(category_payload)
        return await self._fetch_page(1)

    async def _fetch_page(self, page_number: int) -> dict[str, Any]:
        return await self._post_json(
            POSITION_API_URL,
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
            operation=f"page {page_number}",
        )

    async def _post_json(
        self,
        url: str,
        *,
        data: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        await self._rate_limit()
        result = await self.page.evaluate(
            FETCH_JSON_SCRIPT,
            {"url": url, "token": self._csrf_token, "data": data},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"Ant {operation} returned an invalid fetch result")
        status = result.get("status")
        if status != 200:
            raise RuntimeError(f"Ant {operation} returned HTTP {status}")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Ant {operation} returned invalid JSON")
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
