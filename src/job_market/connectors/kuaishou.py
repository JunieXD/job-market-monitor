"""Kuaishou public experienced and daily-internship connector."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
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
POSITION_LIST_URL = "https://zhaopin.kuaishou.cn/#/official/{path}/"
PARTITIONS = ("domestic", "foreign")
MAX_DIRECT_PARTITION_PAGES = 20
# OFFSET ordering is nondeterministic when several jobs share one update time.
# Re-reading only the affected small partition lets the ID union converge while
# the declared source total remains unchanged; failure is still explicit.
MAX_PARTITION_ATTEMPTS = 10


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    path: str
    position_nature_code: str
    employment_type_name: str
    recruit_project: str | None = None

    def page_url(self, partition: str, category_code: str | None = None) -> str:
        url = POSITION_LIST_URL.format(path=self.path) + f"?workLocationCode={partition}"
        if category_code is not None:
            url += f"&positionCategoryCode={category_code}"
        return url


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        path="social",
        position_nature_code="C001",
        employment_type_name="社会招聘",
        recruit_project="socialr",
    ),
    Channel.INTERNSHIP: PortalConfig(
        channel=Channel.INTERNSHIP,
        path="trainee",
        position_nature_code="C002",
        employment_type_name="日常实习",
    ),
}


@dataclass(frozen=True)
class SourceDictionaries:
    locations: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, Any]]
    experiences: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class SourceLabels:
    category_codes: tuple[str, ...]
    location_codes: dict[str, tuple[str, ...]]


class KuaishouConnector:
    """Collect signed responses produced by Kuaishou's public list page."""

    source_key = "kuaishou_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._dictionary_responses: JsonResponseQueue = asyncio.Queue()
        self._label_responses: JsonResponseQueue = asyncio.Queue()
        self._dictionaries: SourceDictionaries | None = None
        self._labels: SourceLabels | None = None
        self._active_location_code: str | None = None
        self._active_nature_code: str | None = None
        self._active_category_code: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Kuaishou connector supports experienced and internship channels")

        jobs_by_id: dict[str, JobRecord] = {}
        partition_counts: dict[str, int] = {}
        overlap_count = 0
        complete = True

        for partition in PARTITIONS:
            root_payload = await self._open_partition(portal, partition)
            root = self._position_page(root_payload, expected_page=1)
            partition_counts[partition] = root["total"]
            self._save_payload(channel, f"{partition}-root", 1, root_payload)
            if root["total"] == 0:
                continue

            category_total = 0
            for category_code in self._require_labels().category_codes:
                if max_pages is not None and self.pages_fetched >= max_pages:
                    complete = False
                    break

                category_partition = f"{partition}-category-{category_code}"
                payload = await self._open_partition(
                    portal,
                    partition,
                    category_code,
                )
                first = self._position_page(payload, expected_page=1)
                partition_counts[category_partition] = first["total"]
                category_total += first["total"]
                category_jobs: dict[str, JobRecord] = {}

                if first["pages"] <= MAX_DIRECT_PARTITION_PAGES:
                    category_jobs, category_complete = await self._collect_pages(
                        portal,
                        channel,
                        category_partition,
                        partition,
                        category_code,
                        payload,
                        first,
                        max_pages,
                    )
                    if not category_complete:
                        complete = False
                else:
                    self._save_payload(
                        channel,
                        f"{category_partition}-root",
                        1,
                        payload,
                    )
                    locations = self._require_labels().location_codes.get(
                        partition,
                        (),
                    )
                    if not locations:
                        raise RuntimeError(
                            f"Kuaishou cannot split large partition {category_partition}"
                        )
                    for location_code in locations:
                        if max_pages is not None and self.pages_fetched >= max_pages:
                            complete = False
                            break
                        child_partition = (
                            f"{category_partition}-location-{location_code}"
                        )
                        child_payload = await self._open_partition(
                            portal,
                            location_code,
                            category_code,
                        )
                        child_first = self._position_page(
                            child_payload,
                            expected_page=1,
                        )
                        partition_counts[child_partition] = child_first["total"]
                        child_jobs, child_complete = await self._collect_pages(
                            portal,
                            channel,
                            child_partition,
                            location_code,
                            category_code,
                            child_payload,
                            child_first,
                            max_pages,
                        )
                        self._merge_records(
                            category_jobs,
                            child_jobs,
                            scope=category_partition,
                        )
                        if not child_complete:
                            complete = False
                            break
                    if complete and len(category_jobs) != first["total"]:
                        raise RuntimeError(
                            f"Kuaishou {category_partition} location coverage mismatch: "
                            f"declared={first['total']}, union={len(category_jobs)}"
                        )

                overlap_count += self._merge_records(
                    jobs_by_id,
                    category_jobs,
                    scope="location partitions",
                )
                if not complete:
                    break

            if not complete:
                break
            if category_total != root["total"]:
                raise RuntimeError(
                    f"Kuaishou {partition} category coverage mismatch: "
                    f"root={root['total']}, category_sum={category_total}"
                )

        if overlap_count:
            partition_counts["cross-partition-overlap"] = overlap_count

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_pages(
        self,
        portal: PortalConfig,
        channel: Channel,
        partition: str,
        work_location_code: str,
        category_code: str,
        payload: dict[str, Any],
        first: dict[str, Any],
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], bool]:
        union_by_id: dict[str, JobRecord] = {}
        target_total = first["total"]

        for attempt in range(1, MAX_PARTITION_ATTEMPTS + 1):
            if attempt > 1:
                payload = await self._open_partition(
                    portal,
                    work_location_code,
                    category_code,
                )
                first = self._position_page(payload, expected_page=1)
                if first["total"] != target_total:
                    target_total = first["total"]
                    union_by_id.clear()

            attempt_partition = (
                partition if attempt == 1 else f"{partition}-retry-{attempt}"
            )
            pass_by_id: dict[str, JobRecord] = {}
            if first["pages"] == 0:
                self._save_payload(channel, attempt_partition, 1, payload)
            for page_number in range(1, first["pages"] + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return pass_by_id, False

                current = self._position_page(payload, expected_page=page_number)
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    attempt_partition,
                    page_number,
                    payload,
                )
                dictionaries = self._require_dictionaries()
                for raw in current["rows"]:
                    record = self.parse_job(raw, portal, dictionaries)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Kuaishou changed job {record.external_id} within "
                            f"{attempt_partition}"
                        )
                    pass_by_id[record.external_id] = record

                if page_number < first["pages"]:
                    payload = await self._next_page(page_number + 1)

            for external_id, record in pass_by_id.items():
                union_by_id[external_id] = record
            if len(union_by_id) == target_total:
                return union_by_id, True
            if len(union_by_id) > target_total:
                union_by_id = pass_by_id

        raise RuntimeError(
            f"Kuaishou {partition} did not converge after "
            f"{MAX_PARTITION_ATTEMPTS} attempts: "
            f"declared={target_total}, union={len(union_by_id)}"
        )

    @staticmethod
    def _merge_records(
        target: dict[str, JobRecord],
        incoming: dict[str, JobRecord],
        *,
        scope: str,
    ) -> int:
        overlap_count = 0
        for external_id, record in incoming.items():
            previous = target.get(external_id)
            if previous is None:
                target[external_id] = record
                continue
            if previous.content_hash() != record.content_hash():
                raise RuntimeError(
                    f"Kuaishou returned conflicting content for job {external_id} "
                    f"across {scope}"
                )
            overlap_count += 1
        return overlap_count

    async def _open_partition(
        self,
        portal: PortalConfig,
        partition: str,
        category_code: str | None = None,
    ) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        if self._dictionaries is None:
            drain_json_responses(self._dictionary_responses)
        if self._labels is None:
            drain_json_responses(self._label_responses)
        self._active_location_code = partition
        self._active_nature_code = portal.position_nature_code
        self._active_category_code = category_code
        await self.page.goto(
            portal.page_url(partition, category_code),
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_position_payload(
            f"positions:{portal.channel.value}:{partition}:1"
        )
        if self._dictionaries is None:
            dictionary_payload = await next_json_payload(
                self._dictionary_responses,
                timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
                operation="Kuaishou source dictionaries",
            )
            self._dictionaries = self.parse_dictionaries(dictionary_payload)
            self._save_dictionary(portal.channel, dictionary_payload)
        if self._labels is None:
            label_payload = await next_json_payload(
                self._label_responses,
                timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
                operation="Kuaishou active filter labels",
            )
            self._labels = self.parse_labels(
                label_payload,
                self._require_dictionaries(),
            )
            self._save_labels(portal.channel, label_payload)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.locator(".ant-pagination-next")
        if await next_button.count() != 1:
            raise RuntimeError("Kuaishou pagination has no unique next button")
        classes = await next_button.get_attribute("class") or ""
        aria_disabled = await next_button.get_attribute("aria-disabled")
        if "ant-pagination-disabled" in classes or aria_disabled == "true":
            raise RuntimeError(f"Kuaishou pagination ended before page {page_number}")
        await next_button.click()
        return await self._next_position_payload(f"positions:{page_number}")

    async def _next_position_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Kuaishou response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("result"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Kuaishou response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Kuaishou response result is not an object")
        total = _response_int(result.get("total"), "total", allow_zero=True)
        page_number = _response_int(result.get("pageNum"), "pageNum")
        page_size = _response_int(result.get("pageSize"), "pageSize")
        pages = _response_int(result.get("pages"), "pages", allow_zero=True)
        rows = result.get("list")
        if rows is None and total == 0:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError("Kuaishou response has no job list")
        if page_number != expected_page:
            raise RuntimeError(
                f"Kuaishou page mismatch: expected={expected_page}, got={page_number}"
            )
        if pages != (total + page_size - 1) // page_size:
            raise RuntimeError(
                "Kuaishou pagination metadata is inconsistent: "
                f"pages={pages}, total={total}, size={page_size}"
            )
        expected_rows = min(page_size, max(total - (page_number - 1) * page_size, 0))
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Kuaishou page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Kuaishou page {page_number} contains a non-object row"
                )
        return {"rows": rows, "total": total, "pages": pages}

    @staticmethod
    def parse_dictionaries(payload: dict[str, Any]) -> SourceDictionaries:
        if payload.get("code") != 0 or not isinstance(payload.get("result"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Kuaishou dictionary response: {message}")
        result = payload["result"]
        return SourceDictionaries(
            locations=_dictionary_items(result.get("workLocation"), "workLocation"),
            categories=_dictionary_items(
                result.get("positionCategory"),
                "positionCategory",
            ),
            experiences=_dictionary_items(
                result.get("positionExperience"),
                "positionExperience",
            ),
        )

    @staticmethod
    def parse_labels(
        payload: dict[str, Any],
        dictionaries: SourceDictionaries,
    ) -> SourceLabels:
        if payload.get("code") != 0 or not isinstance(payload.get("result"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Kuaishou label response: {message}")
        categories = _dictionary_items(payload["result"].get("category"), "category")
        for code, item in categories.items():
            dictionary_item = dictionaries.categories.get(code)
            if dictionary_item is None:
                raise RuntimeError(f"Kuaishou active category {code!r} is unknown")
            if _optional(dictionary_item.get("name")) != _optional(item.get("name")):
                raise RuntimeError(f"Kuaishou active category {code!r} changed name")
        location_codes: dict[str, tuple[str, ...]] = {}
        for partition in PARTITIONS:
            raw_items = payload["result"].get(partition)
            items = (
                {}
                if raw_items is None
                else _dictionary_items(raw_items, partition)
            )
            for code, item in items.items():
                dictionary_item = dictionaries.locations.get(code)
                if dictionary_item is None:
                    raise RuntimeError(
                        f"Kuaishou active location {code!r} is unknown"
                    )
                if _optional(dictionary_item.get("name")) != _optional(
                    item.get("name")
                ):
                    raise RuntimeError(
                        f"Kuaishou active location {code!r} changed name"
                    )
            location_codes[partition] = tuple(items)
        return SourceLabels(
            category_codes=tuple(categories),
            location_codes=location_codes,
        )

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        portal: PortalConfig,
        dictionaries: SourceDictionaries,
    ) -> JobRecord:
        external_id = _required_id(raw.get("id"))
        title = _required(raw, "name", f"Kuaishou job title ({external_id})")
        description = _required(
            raw,
            "description",
            f"Kuaishou job description ({external_id})",
        )
        requirements = _required(
            raw,
            "positionDemand",
            f"Kuaishou job requirements ({external_id})",
        )
        nature_code = _required(
            raw,
            "positionNatureCode",
            f"Kuaishou job nature ({external_id})",
        )
        if nature_code != portal.position_nature_code:
            raise ValueError(
                f"Kuaishou job {external_id} has nature {nature_code!r}; "
                f"expected {portal.position_nature_code!r}"
            )

        category_code = _required(
            raw,
            "positionCategoryCode",
            f"Kuaishou job category ({external_id})",
        )
        category = _lookup_dictionary(
            dictionaries.categories,
            category_code,
            "category",
            external_id,
        )
        experience_code = _optional(raw.get("workExperienceCode"))
        experience_name = None
        if experience_code is not None:
            experience_name = _required_name(
                _lookup_dictionary(
                    dictionaries.experiences,
                    experience_code,
                    "experience",
                    external_id,
                ),
                "experience",
                experience_code,
            )
        experience_min, experience_max = _experience_range(experience_name)

        return JobRecord(
            source_key="kuaishou_cn",
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=(
                f"https://zhaopin.kuaishou.cn/#/official/{portal.path}/"
                f"job-info/{external_id}"
            ),
            company_name="快手",
            channel=portal.channel,
            employment_type_id=nature_code,
            employment_type_name=portal.employment_type_name,
            recruitment_project_id=_optional(raw.get("recruitProjectCode")),
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_datetime(raw.get("releaseTime"), "releaseTime"),
            source_updated_at=_source_datetime(raw.get("updateTime"), "updateTime"),
            source_status=_optional(raw.get("positionStatusCode")),
            recruitment_count=_optional_non_negative_int(
                raw.get("recruitNumber"),
                "recruitNumber",
            ),
            degree_code=_optional(raw.get("educationLimitCode")),
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            department_code=_optional(raw.get("departmentCode")),
            department_name=_optional(raw.get("departmentName")),
            is_hot=_optional_bool(raw.get("ifRecruitWebsiteHot")),
            locations=_locations(raw, dictionaries.locations, external_id),
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=_required_name(category, "category", category_code),
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if "/recruit/e/api/v1/dictionary/batch" in response.url:
            if self._dictionaries is None:
                enqueue_json_response(self._dictionary_responses, response)
            return
        if "/recruit/e/api/v1/open/positions/label" in response.url:
            if self._labels is None:
                enqueue_json_response(self._label_responses, response)
            return
        if "/recruit/e/api/v1/open/positions/simple" not in response.url:
            return
        query = parse_qs(urlsplit(response.url).query)
        if query.get("positionNatureCode") != [self._active_nature_code]:
            return
        if query.get("workLocationCode") != [self._active_location_code]:
            return
        category_code = query.get("positionCategoryCode", [None])
        if category_code != [self._active_category_code]:
            return
        enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.kuaishou_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _require_dictionaries(self) -> SourceDictionaries:
        if self._dictionaries is None:
            raise RuntimeError("Kuaishou source dictionaries have not been loaded")
        return self._dictionaries

    def _require_labels(self) -> SourceLabels:
        if self._labels is None:
            raise RuntimeError("Kuaishou active filter labels have not been loaded")
        return self._labels

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

    def _save_dictionary(self, channel: Channel, payload: dict[str, Any]) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="dictionary",
                    offset=0,
                    payload=payload,
                )
            )

    def _save_labels(self, channel: Channel, payload: dict[str, Any]) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="active-filter-labels",
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


def _required_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Kuaishou job id is invalid: {value!r}")
    text = str(value).strip()
    if not text or (isinstance(value, int) and value < 1):
        raise ValueError(f"Kuaishou job id is invalid: {value!r}")
    return text


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Kuaishou {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Kuaishou {field} is not an integer: {value!r}")
    return value


def _optional_non_negative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Kuaishou {field} is not a non-negative integer: {value!r}")
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"Kuaishou hot flag is not boolean: {value!r}")


def _source_datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Kuaishou {field} is not an ISO datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _dictionary_items(value: Any, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError(f"Kuaishou dictionary {field} is not a list")
    items: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"Kuaishou dictionary {field} contains a non-object")
        code = _optional(item.get("code"))
        name = _optional(item.get("name"))
        if code is None or name is None:
            raise RuntimeError(f"Kuaishou dictionary {field} has an unnamed item")
        if code in items:
            raise RuntimeError(f"Kuaishou dictionary {field} repeated code {code!r}")
        items[code] = item
    if not items:
        raise RuntimeError(f"Kuaishou dictionary {field} is empty")
    return items


def _lookup_dictionary(
    dictionary: dict[str, dict[str, Any]],
    code: str,
    label: str,
    external_id: str,
) -> dict[str, Any]:
    item = dictionary.get(code)
    if item is None:
        raise ValueError(f"Kuaishou job {external_id} has unknown {label} code {code!r}")
    return item


def _required_name(item: dict[str, Any], label: str, code: str) -> str:
    name = _optional(item.get("name"))
    if name is None:
        raise ValueError(f"Kuaishou {label} code {code!r} has no name")
    return name


def _locations(
    raw: dict[str, Any],
    dictionary: dict[str, dict[str, Any]],
    external_id: str,
) -> list[LocationRecord]:
    value = raw.get("workLocationsCode")
    if value is None:
        fallback = _optional(raw.get("workLocationCode"))
        value = [fallback] if fallback is not None else []
    if not isinstance(value, list):
        raise ValueError(f"Kuaishou job {external_id} has invalid work locations")
    codes = []
    for raw_code in value:
        code = _optional(raw_code)
        if code is not None and code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError(f"Kuaishou job {external_id} has no work location")
    return [
        LocationRecord(
            code=code,
            name=_required_name(
                _lookup_dictionary(dictionary, code, "location", external_id),
                "location",
                code,
            ),
        )
        for code in codes
    ]


def _experience_range(name: str | None) -> tuple[int | None, int | None]:
    if name is None:
        return None, None
    match = re.fullmatch(r"(\d+)\s*[-~至]\s*(\d+)年", name)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"(\d+)年(?:以上|及以上)", name)
    if match:
        return int(match.group(1)), None
    match = re.fullmatch(r"(\d+)年(?:以下|以内)", name)
    if match:
        return 0, int(match.group(1))
    return None, None
