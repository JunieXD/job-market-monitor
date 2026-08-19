"""NetEase public mixed-position page connector."""

import asyncio
import json
import re
from datetime import UTC, datetime
from math import ceil
from typing import Any

from playwright.async_api import Page

from job_market.config import Settings
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

POSITION_LIST_URL = "https://hr.163.com/job-list.html"
POSITION_API_URL = "https://hr.163.com/api/hr163/position/queryPage"
COLLECTION_PAGE_SIZE = 200
MAX_COLLECTION_ATTEMPTS = 5
WORK_TYPES = {"0": "全职", "1": "实习", "2": "派遣"}


class NetEaseConnector:
    """Collect the official NetEase page that mixes several work types."""

    source_key = "netease_cn"

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
        if channel is not Channel.GENERAL:
            raise ValueError("NetEase connector supports only the general channel")

        initial_payload = await self._open_first_page()
        initial_total = self._page_data(initial_payload, 1)["total"]
        jobs_by_id, total_count, complete, observations = await self._collect_root(
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
                "all": total_count,
                "collected-unique": len(jobs_by_id),
                **{
                    f"root-observation-{index:02d}": total
                    for index, total in enumerate(observations, start=1)
                },
            },
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(self) -> dict[str, Any]:
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        return await self._fetch_page(1)

    async def _fetch_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        result = await self.page.evaluate(
            """async ({url, currentPage, pageSize}) => {
                const response = await fetch(url, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "language": "zh",
                        "authtype": "ursAuth",
                    },
                    body: JSON.stringify({currentPage, pageSize}),
                    credentials: "same-origin",
                });
                return {status: response.status, payload: await response.json()};
            }""",
            {
                "url": POSITION_API_URL,
                "currentPage": page_number,
                "pageSize": COLLECTION_PAGE_SIZE,
            },
        )
        if not isinstance(result, dict) or result.get("status") != 200:
            status = result.get("status") if isinstance(result, dict) else None
            raise RuntimeError(f"NetEase page {page_number} returned HTTP {status}")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"NetEase page {page_number} returned invalid JSON")
        if payload.get("code") != 200 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(
                f"Invalid NetEase response for page {page_number}: {message}"
            )
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _collect_root(
        self,
        channel: Channel,
        initial_payload: dict[str, Any],
        initial_total: int,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool, list[int]]:
        union_by_id: dict[str, JobRecord] = {}
        target_total = initial_total
        observations: list[int] = []

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = initial_payload if attempt == 1 else await self._fetch_page(1)
            first = self._page_data(payload, 1)
            observations.append(first["total"])
            if first["total"] != target_total:
                target_total = first["total"]
                union_by_id = {}

            pass_by_id: dict[str, JobRecord] = {}
            total_pages = first["pages"]
            partition = "root" if attempt == 1 else f"root-retry-{attempt}"
            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, target_total, False, observations
                current = self._page_data(payload, page_number)
                if current["total"] != target_total or current["pages"] != total_pages:
                    break
                self.pages_fetched += 1
                self._save_payload(channel, partition, page_number, payload)
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            "NetEase returned conflicting content for job "
                            f"{record.external_id}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._fetch_page(page_number + 1)
            else:
                for external_id, record in pass_by_id.items():
                    previous = union_by_id.get(external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"NetEase job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True, observations
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        "NetEase observations exceeded the declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            "NetEase root did not converge after "
            f"{MAX_COLLECTION_ATTEMPTS} attempts: declared={target_total}, "
            f"union={len(union_by_id)}"
        )

    @staticmethod
    def _page_data(payload: dict[str, Any], expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("NetEase response data is not an object")
        rows = data.get("list")
        pages = data.get("pages")
        total = data.get("total")
        if not isinstance(rows, list):
            raise RuntimeError("NetEase response has no job list")
        if not isinstance(pages, int) or pages < 0:
            raise RuntimeError(f"NetEase returned invalid pages: {pages!r}")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError(f"NetEase returned invalid total: {total!r}")
        expected_pages = ceil(total / COLLECTION_PAGE_SIZE) if total else 0
        if pages != expected_pages:
            raise RuntimeError(
                "NetEase pagination metadata mismatch: "
                f"pages={pages}, total={total}, size={COLLECTION_PAGE_SIZE}"
            )
        expected_rows = min(
            COLLECTION_PAGE_SIZE,
            max(total - (expected_page - 1) * COLLECTION_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"NetEase page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        if total and not rows:
            raise RuntimeError(f"NetEase returned an empty non-terminal page {expected_page}")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"NetEase page {expected_page} contains a non-object row")
        return {"rows": rows, "pages": pages, "total": total}

    async def _rate_limit(self) -> None:
        delay = self.settings.netease_request_delay_seconds - (
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

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "id", "NetEase job id")
        title = _required(raw, "name", f"NetEase job title ({external_id})")
        description = _required(raw, "description", f"NetEase job description ({external_id})")
        requirements = _required(raw, "requirement", f"NetEase job requirement ({external_id})")
        work_type_id = _required(raw, "workType", f"NetEase work type ({external_id})")
        try:
            work_type_name = WORK_TYPES[work_type_id]
        except KeyError as exc:
            raise ValueError(f"NetEase exposed an unknown workType: {work_type_id!r}") from exc

        category_name = _optional(raw.get("firstPostTypeName"))
        categories = []
        if category_name is not None:
            categories.append(
                SourceCategoryRecord(
                    external_id=f"firstPostType:{category_name}",
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            )

        product_name = _optional(raw.get("productName"))
        business_units = []
        if product_name is not None:
            business_units.append(
                BusinessUnitRecord(
                    code=_optional(raw.get("product")) or f"product:{product_name}",
                    name=product_name,
                )
            )

        locations = _locations(raw.get("workPlaceNameList"), raw.get("workPlaceList"), external_id)
        experience_min, experience_max = _experience_range(raw.get("reqWorkYearsName"))
        degree_name = _optional(raw.get("reqEducationName"))
        return JobRecord(
            source_key="netease_cn",
            external_id=external_id,
            source_url=f"{POSITION_LIST_URL}?jobId={external_id}",
            company_name="网易",
            channel=Channel.GENERAL,
            employment_type_id=work_type_id,
            employment_type_name=work_type_name,
            title=title,
            description=description,
            requirements=requirements,
            source_updated_at=_timestamp_ms(raw.get("updateTime"), "updateTime"),
            recruitment_count=_optional_non_negative_int(
                raw.get("recruitNum"),
                "recruitNum",
            ),
            degree_name=degree_name,
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            department_name=_optional(raw.get("firstDepName")),
            locations=locations,
            categories=categories,
            business_units=business_units,
            source_payload=raw,
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


def _optional_non_negative_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"NetEase {field} is not a non-negative integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"NetEase {field} is not a non-negative integer: {value!r}"
        ) from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError(f"NetEase {field} is not a non-negative integer: {value!r}")
    return parsed


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"NetEase {field} is not a numeric timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _locations(names_value: Any, codes_value: Any, external_id: str) -> list[LocationRecord]:
    if not isinstance(names_value, list):
        raise ValueError(f"NetEase job has no workPlaceNameList: {external_id}")
    codes = codes_value if isinstance(codes_value, list) else []
    locations: list[LocationRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(names_value):
        name = _optional(item)
        if name is None:
            continue
        code = _optional(codes[index]) if index < len(codes) else None
        code = code or f"city:{name}"
        if code in seen:
            continue
        seen.add(code)
        locations.append(LocationRecord(code=code, name=name))
    if not locations:
        raise ValueError(f"NetEase job has no work location: {external_id}")
    return locations


def _experience_range(value: Any) -> tuple[int | None, int | None]:
    text = _optional(value)
    if text is None or text in {"不限", "经验不限"}:
        return None, None
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)\s*年", text)
    if match:
        minimum, maximum = (int(item) for item in match.groups())
        if minimum <= maximum:
            return minimum, maximum
        raise ValueError(f"NetEase work-year range is reversed: {text!r}")
    match = re.fullmatch(r"(\d+)\s*年以上", text)
    if match:
        return int(match.group(1)), None
    # Keep an unfamiliar official label in source_payload rather than guessing.
    return None, None
