"""Beike public experienced-recruitment connector."""

import asyncio
import json
import re
from datetime import UTC, datetime
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
PAGE_SIZE = 20
MAX_COLLECTION_ATTEMPTS = 5
POSITION_LIST_URL = "https://join.ke.com/social/jobs"
POSITION_DETAIL_URL = "https://join.ke.com/social/detail?jobAdId={external_id}"
POSITION_ENDPOINT = "/api/Jobad/GetJobAdPageList"
DISPLAY_FIELDS = [
    "Category",
    "LocId",
    "Org",
    "Station",
    "PostDate",
    "Degree",
    "YearsOfWorking",
    "WorkWeChatQrCode",
]


class BeikeConnector:
    """Collect Beike's root list and official first-level category filters."""

    source_key = "beike_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page_index = 0
        self._active_category_id: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Beike connector supports only experienced jobs")

        jobs_by_id, root_total, complete = await self._collect_root(max_pages)
        partition_counts = {
            "all": root_total,
            "collected-unique": len(jobs_by_id),
        }
        if not complete:
            return self._result(jobs_by_id, partition_counts, complete=False)

        catalog = await self._category_catalog()
        assignments: dict[str, list[SourceCategoryRecord]] = {
            external_id: [] for external_id in jobs_by_id
        }
        for category_id, category_name in catalog:
            ids, total, category_complete = await self._collect_category_ids(
                category_id,
                jobs_by_id,
                max_pages,
            )
            partition_counts[f"category:{category_id}"] = total
            assignment = SourceCategoryRecord(
                external_id=f"classification-one:{category_id}",
                name=category_name,
                assignment_method=CategoryAssignmentMethod.FILTER_MEMBERSHIP,
            )
            for external_id in ids:
                assignments[external_id].append(assignment)
            if not category_complete:
                return self._result(
                    jobs_by_id,
                    partition_counts,
                    assignments,
                    complete=False,
                )

        self._save_payload(
            Channel.EXPERIENCED,
            "category-catalog",
            0,
            {
                "categories": [
                    {"id": category_id, "name": name}
                    for category_id, name in catalog
                ]
            },
        )
        partition_counts["category-catalog"] = len(catalog)
        return self._result(
            jobs_by_id,
            partition_counts,
            assignments,
            complete=True,
        )

    async def _collect_root(
        self,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool]:
        union: dict[str, JobRecord] = {}
        target_total: int | None = None

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = await self._open_partition(None)
            first = self._position_page(payload, expected_page_index=0)
            if target_total != first["total"]:
                target_total = first["total"]
                union = {}
            pages = ceil(target_total / PAGE_SIZE) if target_total else 0
            current_pass: dict[str, JobRecord] = {}
            partition = "root" if attempt == 1 else f"root-retry-{attempt}"

            for page_index in range(pages):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union or current_pass, target_total, False
                current = self._position_page(
                    payload,
                    expected_page_index=page_index,
                )
                if current["total"] != target_total:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    Channel.EXPERIENCED,
                    partition,
                    page_index,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = current_pass.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Beike job {record.external_id} changed within {partition}"
                        )
                    current_pass[record.external_id] = record
                if page_index + 1 < pages:
                    payload = await self._next_page(page_index + 1)
            else:
                self._merge_records(union, current_pass)
                if len(union) == target_total:
                    return union, target_total, True
                if len(union) > target_total:
                    raise RuntimeError(
                        "Beike observations exceeded the declared total: "
                        f"{len(union)} > {target_total}"
                    )

        raise RuntimeError(
            "Beike root list did not converge: "
            f"declared={target_total}, unique={len(union)}"
        )

    async def _collect_category_ids(
        self,
        category_id: str,
        jobs_by_id: dict[str, JobRecord],
        max_pages: int | None,
    ) -> tuple[set[str], int, bool]:
        union: set[str] = set()
        target_total: int | None = None

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = await self._open_partition(category_id)
            first = self._position_page(payload, expected_page_index=0)
            if target_total != first["total"]:
                target_total = first["total"]
                union = set()
            pages = ceil(target_total / PAGE_SIZE) if target_total else 0
            current_pass: set[str] = set()
            partition = f"category-{category_id}-retry-{attempt}"

            for page_index in range(pages):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union or current_pass, target_total, False
                current = self._position_page(
                    payload,
                    expected_page_index=page_index,
                )
                if current["total"] != target_total:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    Channel.EXPERIENCED,
                    partition,
                    page_index,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    root = jobs_by_id.get(record.external_id)
                    if root is None:
                        raise RuntimeError(
                            "Beike category returned a job absent from the root: "
                            f"{record.external_id}"
                        )
                    if root.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Beike job {record.external_id} changed between root "
                            f"and category {category_id}"
                        )
                    current_pass.add(record.external_id)
                if page_index + 1 < pages:
                    payload = await self._next_page(page_index + 1)
            else:
                union.update(current_pass)
                if len(union) == target_total:
                    return union, target_total, True
                if len(union) > target_total:
                    raise RuntimeError(
                        f"Beike category {category_id} exceeded its declared total"
                    )

        raise RuntimeError(
            f"Beike category {category_id} did not converge: "
            f"declared={target_total}, unique={len(union)}"
        )

    async def _open_partition(self, category_id: str | None) -> dict[str, Any]:
        self._active_page_index = 0
        self._active_category_id = None
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        root = await self._next_payload("root:0")
        if category_id is None:
            return root

        self._active_category_id = category_id
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        group = self.page.locator(".phoenix-checkbox-group-container").first
        checkbox = group.locator(
            f'input.phoenix-checkbox__input[value="{category_id}"]'
        )
        if await checkbox.count() != 1:
            raise RuntimeError(f"Beike category {category_id!r} is not unique")
        await checkbox.locator("xpath=../..").click()
        return await self._next_payload(f"category:{category_id}:0")

    async def _category_catalog(self) -> list[tuple[str, str]]:
        await self._open_partition(None)
        group = self.page.locator(".phoenix-checkbox-group-container").first
        checkboxes = group.locator("input.phoenix-checkbox__input")
        catalog: list[tuple[str, str]] = []
        for index in range(await checkboxes.count()):
            checkbox = checkboxes.nth(index)
            category_id = _required_value(
                await checkbox.get_attribute("value"),
                "Beike category id",
            )
            name = _required_value(
                await checkbox.locator("xpath=../../..").inner_text(),
                f"Beike category name ({category_id})",
            )
            catalog.append((category_id, name))
        if not catalog or len({item[0] for item in catalog}) != len(catalog):
            raise RuntimeError("Beike category catalog is empty or duplicated")
        return catalog

    async def _next_page(self, page_index: int) -> dict[str, Any]:
        self._active_page_index = page_index
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        await self.page.evaluate(
            "window.scrollTo(0, document.documentElement.scrollHeight)"
        )
        return await self._next_payload(f"positions:{page_index}")

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Beike response: {operation}",
        )
        if payload.get("Code") != 200 or not isinstance(payload.get("Data"), list):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Beike response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page_index: int,
    ) -> dict[str, Any]:
        total = _response_int(payload.get("Count"), "Count", allow_zero=True)
        rows = payload.get("Data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Beike response has invalid Data")
        expected_rows = min(
            PAGE_SIZE,
            max(total - expected_page_index * PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Beike page {expected_page_index} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "Id", "Beike job id")
        title = _required(raw, "JobAdName", f"Beike job title ({external_id})")
        employment_id = _required(
            raw,
            "CategoryId",
            f"Beike recruitment category id ({external_id})",
        )
        employment_name = _required(
            raw,
            "Category",
            f"Beike recruitment category ({external_id})",
        )
        experience_min, experience_max = _experience(raw.get("YearsOfWorking"))
        locations = [
            LocationRecord(code=f"name:{name}", name=name)
            for name in _unique_strings(raw.get("LocNames"), "LocNames")
        ]
        return JobRecord(
            source_key="beike_social_cn",
            external_id=external_id,
            external_code=_optional(raw.get("JobAdId")),
            source_url=POSITION_DETAIL_URL.format(external_id=external_id),
            company_name="贝壳",
            channel=Channel.EXPERIENCED,
            employment_type_id=employment_id,
            employment_type_name=employment_name,
            title=title,
            description=_optional(raw.get("Duty")),
            requirements=_optional(raw.get("Require")),
            published_at=_timestamp_ms(raw.get("PostDateInt"), "PostDateInt"),
            source_updated_at=_source_datetime(raw.get("ChangeDate")),
            source_status=_optional(raw.get("Status")),
            degree_name=_optional(raw.get("Degree")),
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            department_code=_optional(raw.get("OrgId")),
            department_name=_optional(raw.get("Org")),
            is_hot=_optional_bool(raw.get("Channel4IsHot"), "Channel4IsHot"),
            locations=locations,
            categories=[],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200 or POSITION_ENDPOINT not in response.url:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        expected: dict[str, Any] = {
            "PageIndex": self._active_page_index,
            "PageSize": PAGE_SIZE,
            "Category": ["1"],
            "KeyWords": "",
            "SpecialType": 0,
            "PortalId": "",
            "DisplayFields": DISPLAY_FIELDS,
        }
        if self._active_category_id is not None:
            expected["ClassificationOne"] = [int(self._active_category_id)]
        if post_data == expected:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.beike_request_delay_seconds - (
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

    @staticmethod
    def _merge_records(
        target: dict[str, JobRecord],
        incoming: dict[str, JobRecord],
    ) -> None:
        for external_id, record in incoming.items():
            previous = target.get(external_id)
            if previous is not None and previous.content_hash() != record.content_hash():
                raise RuntimeError(
                    f"Beike job {external_id} changed during pagination retries"
                )
            target[external_id] = record

    def _result(
        self,
        jobs_by_id: dict[str, JobRecord],
        partition_counts: dict[str, int],
        assignments: dict[str, list[SourceCategoryRecord]] | None = None,
        *,
        complete: bool,
    ) -> CollectionResult:
        jobs = list(jobs_by_id.values())
        if assignments is not None:
            jobs = [
                job.model_copy(
                    update={
                        "categories": sorted(
                            assignments[job.external_id],
                            key=lambda item: (item.name, item.external_id),
                        )
                    }
                )
                for job in jobs
            ]
        return CollectionResult(
            channel=Channel.EXPERIENCED,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
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


def _required_value(value: Any, label: str) -> str:
    result = _optional(value)
    if result is None:
        raise ValueError(f"{label} is missing")
    return result


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Beike {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Beike {field} is invalid: {value!r}")
    return value


def _unique_strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Beike {field} must be a list")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value in (None, 0):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Beike {field} is not a numeric timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _source_datetime(value: Any) -> datetime | None:
    text = _optional(value)
    if text is None or text.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Beike ChangeDate is invalid: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _experience(value: Any) -> tuple[int | None, int | None]:
    text = _optional(value)
    if text is None or text == "应届毕业生":
        return None, None
    match = re.fullmatch(r"(\d+)年", text)
    if match:
        years = int(match.group(1))
        return years, years
    match = re.fullmatch(r"(\d+)年及以上", text)
    if match:
        return int(match.group(1)), None
    raise ValueError(f"Beike YearsOfWorking is unknown: {text!r}")


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Beike {field} is not boolean: {value!r}")
    return value
