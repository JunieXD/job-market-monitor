"""Kuaishou's public campus graduate and retained-internship portal."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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

BASE_URL = "https://campus.kuaishou.cn/recruit/campus/e"
DICTIONARY_ENDPOINT = f"{BASE_URL}/api/v1/dictionary/batch"
POSITION_ENDPOINT = f"{BASE_URL}/api/v1/open/positions/simple"
PAGE_SIZE = 100
MAX_COLLECTION_ATTEMPTS = 5
RESPONSE_TIMEOUT_SECONDS = 30
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CategoryInfo:
    code: str
    name: str
    parent_code: str | None = None
    parent_name: str | None = None


@dataclass(frozen=True)
class ProjectInfo:
    code: str
    name: str
    nature_code: str
    nature_name: str


class KuaishouCampusConnector:
    """Collect the campus portal's JSON directly in a headless browser context."""

    source_key = "kuaishou_campus_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel not in {Channel.CAMPUS, Channel.INTERNSHIP}:
            raise ValueError("Kuaishou campus connector supports campus and internship channels")

        dictionaries = await self._fetch_dictionaries()
        project = _select_project(dictionaries["recruitSubProject"], channel)
        await self.page.goto(
            f"{BASE_URL}/#/campus/jobs?recruitSubProjectCodes={project.code}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        self._save_payload(channel, "dictionaries", 0, dictionaries)
        categories = _category_catalog(dictionaries.get("positionCategory"))
        locations = _dictionary_catalog(dictionaries.get("workLocation"), "workLocation")
        natures = _dictionary_catalog(dictionaries.get("positionNature"), "positionNature")

        jobs_by_id: dict[str, JobRecord] = {}
        target_total: int | None = None
        partition_counts: dict[str, int] = {}
        complete = False
        last_error: Exception | None = None
        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            try:
                current, total, counts, complete = await self._collect_pass(
                    channel,
                    project,
                    categories,
                    locations,
                    natures,
                    max_pages=max_pages,
                    attempt=attempt,
                )
                partition_counts.update(counts)
                if target_total != total:
                    target_total = total
                    jobs_by_id = {}
                for external_id, record in current.items():
                    previous = jobs_by_id.get(external_id)
                    if previous is not None and previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Kuaishou campus job {external_id} changed during collection"
                        )
                    jobs_by_id[external_id] = record
                if not complete:
                    break
                if target_total == len(jobs_by_id):
                    break
                last_error = RuntimeError(
                    f"Kuaishou campus list did not converge: declared={target_total}, "
                    f"unique={len(jobs_by_id)}"
                )
            except Exception as exc:
                last_error = exc
                if max_pages is not None or attempt == MAX_COLLECTION_ATTEMPTS:
                    raise

        if target_total is None:
            raise RuntimeError("Kuaishou campus returned no pagination metadata")
        if complete and target_total != len(jobs_by_id):
            raise RuntimeError(str(last_error))
        partition_counts["project"] = target_total
        partition_counts["collected-unique"] = len(jobs_by_id)
        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_pass(
        self,
        channel: Channel,
        project: ProjectInfo,
        categories: dict[str, CategoryInfo],
        locations: dict[str, dict[str, Any]],
        natures: dict[str, dict[str, Any]],
        *,
        max_pages: int | None,
        attempt: int,
    ) -> tuple[dict[str, JobRecord], int, dict[str, int], bool]:
        jobs_by_id: dict[str, JobRecord] = {}
        first_payload = await self._fetch_page(project.code, 1)
        first = _position_page(first_payload, expected_page=1)
        total = first["total"]
        pages = first["pages"]
        complete = True
        for page_number in range(1, pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            payload = (
                first_payload
                if page_number == 1
                else await self._fetch_page(project.code, page_number)
            )
            current = _position_page(payload, expected_page=page_number)
            if current["total"] != total:
                raise RuntimeError(
                    f"Kuaishou campus total changed during pass: {total} -> {current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(
                channel,
                "project" if attempt == 1 else f"project-retry-{attempt}",
                page_number - 1,
                payload,
            )
            for raw in current["rows"]:
                record = self.parse_job(raw, project, categories, locations, natures)
                previous = jobs_by_id.get(record.external_id)
                if previous is not None and previous.content_hash() != record.content_hash():
                    raise RuntimeError(
                        f"Kuaishou campus job {record.external_id} changed within a pass"
                    )
                jobs_by_id[record.external_id] = record
        return jobs_by_id, total, {"project": total}, complete

    async def _fetch_dictionaries(self) -> dict[str, Any]:
        payload = await self._request_json(
            f"{DICTIONARY_ENDPOINT}?types=workLocation,positionCategory,positionCategoryFlatten,positionNature,recruitSubProject"
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Kuaishou campus dictionary response has no result")
        return result

    async def _fetch_page(self, project_code: str, page_number: int) -> dict[str, Any]:
        return await self._request_json(
            POSITION_ENDPOINT,
            method="POST",
            body={
                "recruitSubProjectCodes": [project_code],
                "pageSize": PAGE_SIZE,
                "pageNum": page_number,
            },
        )

    async def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        delay = self.settings.kuaishou_campus_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)
        if method == "POST":
            response = await self.page.request.post(
                url,
                data=body,
                timeout=RESPONSE_TIMEOUT_SECONDS * 1000,
            )
        else:
            response = await self.page.request.get(
                url,
                timeout=RESPONSE_TIMEOUT_SECONDS * 1000,
            )
        self._last_request_at = asyncio.get_running_loop().time()
        if response.status != 200:
            raise RuntimeError(
                f"Kuaishou campus endpoint returned HTTP {response.status}: {url}"
            )
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Kuaishou campus response: {message}")
        return payload

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        project: ProjectInfo,
        categories: dict[str, CategoryInfo],
        locations: dict[str, dict[str, Any]],
        natures: dict[str, dict[str, Any]],
    ) -> JobRecord:
        external_id = _required_id(raw.get("id"), "Kuaishou campus job id")
        title = _required(raw, "name", f"Kuaishou campus job title ({external_id})")
        nature_code = _required(
            raw,
            "positionNatureCode",
            f"Kuaishou campus nature ({external_id})",
        )
        if nature_code != project.nature_code:
            raise ValueError(
                f"Kuaishou campus job {external_id} has nature {nature_code!r}; "
                f"expected {project.nature_code!r}"
            )
        category_code = _required(
            raw,
            "positionCategoryCode",
            f"Kuaishou campus category ({external_id})",
        )
        category = categories.get(category_code)
        if category is None:
            raise ValueError(f"Kuaishou campus category {category_code!r} is unknown")
        location_records: list[LocationRecord] = []
        raw_locations = raw.get("workLocationDicts")
        if raw_locations is not None:
            if not isinstance(raw_locations, list):
                raise ValueError(f"Kuaishou campus locations are invalid for {external_id}")
            for item in raw_locations:
                if not isinstance(item, dict):
                    raise ValueError(f"Kuaishou campus location is invalid for {external_id}")
                code = _required(item, "code", f"Kuaishou campus location ({external_id})")
                name = _required(item, "name", f"Kuaishou campus location name ({external_id})")
                dictionary = locations.get(code)
                if dictionary is None or dictionary.get("name") != name:
                    raise ValueError(
                        f"Kuaishou campus location {code!r} is absent or changed"
                    )
                location_records.append(LocationRecord(code=code, name=name))
        nature_name = _required_name(natures, nature_code, project.nature_name)
        return JobRecord(
            source_key="kuaishou_campus_cn",
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=(
                f"{BASE_URL}/#/campus/job-info/{external_id}"
                f"?recruitSubProjectCodes={project.code}"
            ),
            company_name="快手",
            channel=Channel.CAMPUS if project.nature_code == "fulltime" else Channel.INTERNSHIP,
            employment_type_id=nature_code,
            employment_type_name=nature_name,
            recruitment_project_id=project.code,
            recruitment_project_name=project.name,
            title=title,
            description=_optional(raw.get("description")),
            requirements=_optional(raw.get("positionDemand")),
            published_at=_datetime(raw.get("releaseTime"), "releaseTime"),
            source_updated_at=_timestamp(raw.get("updateTime"), "updateTime"),
            source_status=_optional(raw.get("positionStatusCode")),
            recruitment_count=_optional_int(raw.get("recruitNumber"), "recruitNumber"),
            department_code=_optional(raw.get("departmentCode")),
            department_name=_optional(raw.get("departmentName")),
            locations=location_records,
            categories=[
                SourceCategoryRecord(
                    external_id=category.code,
                    name=category.name,
                    parent_external_id=category.parent_code,
                    parent_name=category.parent_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            is_hot=_optional_bool(raw.get("ifRecruitWebsiteHot")),
            source_payload=raw,
        )

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


def _select_project(items: Any, channel: Channel) -> ProjectInfo:
    if not isinstance(items, list):
        raise RuntimeError("Kuaishou campus project dictionary is not a list")
    suffix = "应届生" if channel is Channel.CAMPUS else "实习生"
    candidates = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("ifActive") is True
        and re.fullmatch(r"\d{4}" + suffix, str(item.get("name", "")))
    ]
    if not candidates:
        raise RuntimeError(f"Kuaishou campus has no active {suffix} project")
    selected = max(
        candidates,
        key=lambda item: (int(item.get("sortId", -1)), str(item.get("code"))),
    )
    code = _required(selected, "code", "Kuaishou campus project code")
    name = _required(selected, "name", "Kuaishou campus project name")
    nature_code = "fulltime" if channel is Channel.CAMPUS else "intern"
    nature_name = "全职" if channel is Channel.CAMPUS else "实习"
    return ProjectInfo(code=code, name=name, nature_code=nature_code, nature_name=nature_name)


def _dictionary_catalog(value: Any, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError(f"Kuaishou campus {field} dictionary is not a list")
    catalog: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"Kuaishou campus {field} dictionary has a non-object")
        code = _optional(item.get("code"))
        if code is None or code in catalog:
            raise RuntimeError(f"Kuaishou campus {field} dictionary has an invalid code")
        catalog[code] = item
    if not catalog:
        raise RuntimeError(f"Kuaishou campus {field} dictionary is empty")
    return catalog


def _category_catalog(value: Any) -> dict[str, CategoryInfo]:
    catalog: dict[str, CategoryInfo] = {}
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict):
            raise RuntimeError("Kuaishou campus category dictionary has a non-object")
        code = _optional(item.get("code"))
        name = _optional(item.get("name"))
        if code is None or name is None:
            raise RuntimeError("Kuaishou campus category dictionary has an unnamed item")
        if code in catalog:
            raise RuntimeError(f"Kuaishou campus category {code!r} is duplicated")
        catalog[code] = CategoryInfo(code=code, name=name)
        children = item.get("children")
        if children is None:
            continue
        if not isinstance(children, list):
            raise RuntimeError(f"Kuaishou campus category {code!r} children are invalid")
        for child in children:
            if not isinstance(child, dict):
                raise RuntimeError("Kuaishou campus category child is invalid")
            child_code = _optional(child.get("code"))
            child_name = _optional(child.get("name"))
            if child_code is None or child_name is None or child_code in catalog:
                raise RuntimeError("Kuaishou campus category child is invalid or duplicated")
            catalog[child_code] = CategoryInfo(child_code, child_name, code, name)
    if not catalog:
        raise RuntimeError("Kuaishou campus category dictionary is empty")
    return catalog


def _required_name(catalog: dict[str, dict[str, Any]], code: str, fallback: str) -> str:
    item = catalog.get(code)
    if item is None or _optional(item.get("name")) is None:
        raise ValueError(f"Kuaishou campus nature {code!r} is unknown")
    return str(item["name"])


def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Kuaishou campus response has no result object")
    total = _required_int(result.get("total"), "total", allow_zero=True)
    page = _required_int(result.get("pageNum"), "pageNum")
    page_size = _required_int(result.get("pageSize"), "pageSize")
    pages = _required_int(result.get("pages"), "pages", allow_zero=True)
    rows = result.get("list")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("Kuaishou campus response has an invalid list")
    if page != expected_page or pages != ((total + page_size - 1) // page_size if total else 0):
        raise RuntimeError("Kuaishou campus pagination metadata is inconsistent")
    expected_rows = min(page_size, max(total - (page - 1) * page_size, 0))
    if len(rows) != expected_rows:
        raise RuntimeError(
            f"Kuaishou campus page {page} row mismatch: expected={expected_rows}, got={len(rows)}"
        )
    return {"rows": rows, "total": total, "pages": pages}


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


def _required_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} is invalid: {value!r}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is invalid: {value!r}")
    return text


def _required_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Kuaishou campus {field} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Kuaishou campus {field} is not an integer: {value!r}") from exc
    if parsed < (0 if allow_zero else 1):
        raise RuntimeError(f"Kuaishou campus {field} is invalid: {value!r}")
    return parsed


def _datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Kuaishou campus {field} is invalid: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"Kuaishou campus {field} is invalid: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=SHANGHAI)


def _optional_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Kuaishou campus {field} is invalid: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Kuaishou campus {field} is invalid: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"Kuaishou campus {field} is invalid: {value!r}")
    return parsed


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # The current portal reuses this field for a millisecond timestamp on
    # some projects. Until the source contract documents that meaning, keep
    # the raw value in source_payload and leave the normalized flag unknown.
    return None
