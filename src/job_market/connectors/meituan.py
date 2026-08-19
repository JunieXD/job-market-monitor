"""Meituan public recruitment portal connector.

The portal's list cards already contain the responsibility and requirement
texts.  This connector navigates its public social/campus pages and retains
only the JSON responses that those pages issue themselves.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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

POSITION_LIST_URL = "https://zhaopin.meituan.com/web/{path}"
POSITION_API_URL = "https://zhaopin.meituan.com/api/official/job/getJobList"
COLLECTION_PAGE_SIZE = 200
MAX_COLLECTION_ATTEMPTS = 5


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    path: str
    employment_type_name: str
    job_type_codes: tuple[str, ...]

    @property
    def page_url(self) -> str:
        return POSITION_LIST_URL.format(path=self.path)


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        path="social",
        employment_type_name="社会招聘",
        job_type_codes=("3",),
    ),
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        path="campus",
        employment_type_name="校园招聘",
        job_type_codes=("1", "2"),
    ),
}


class MeituanConnector:
    """Collect public Meituan social and campus job-list responses."""

    source_key = "meituan_cn"

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
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Meituan connector supports campus and experienced channels")

        await self.page.goto(
            POSITION_API_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        initial_payload = await self._fetch_page(portal, 1)
        initial_total = self._position_page(initial_payload, expected_page=1)[
            "total_count"
        ]
        jobs_by_id, total_count, complete, observations = await self._collect_root(
            portal,
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

    async def _fetch_page(
        self,
        portal: PortalConfig,
        page_number: int,
    ) -> dict[str, Any]:
        await self._rate_limit()
        result = await self.page.evaluate(
            """async ({url, pageNo, pageSize, jobTypeCodes}) => {
                const response = await fetch(url, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        page: {pageNo, pageSize},
                        jobShareType: "1",
                        keywords: "",
                        cityList: [],
                        department: [],
                        jfJgList: [],
                        jobType: jobTypeCodes.map(code => ({code, subCode: []})),
                        typeCode: [],
                        specialCode: [],
                    }),
                    credentials: "same-origin",
                });
                return {status: response.status, payload: await response.json()};
            }""",
            {
                "url": POSITION_API_URL,
                "pageNo": page_number,
                "pageSize": COLLECTION_PAGE_SIZE,
                "jobTypeCodes": list(portal.job_type_codes),
            },
        )
        if not isinstance(result, dict) or result.get("status") != 200:
            status = result.get("status") if isinstance(result, dict) else None
            raise RuntimeError(f"Meituan page {page_number} returned HTTP {status}")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Meituan page {page_number} returned invalid JSON")
        if payload.get("status") != 1 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Meituan position response: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _collect_root(
        self,
        portal: PortalConfig,
        initial_payload: dict[str, Any],
        initial_total: int,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool, list[int]]:
        union_by_id: dict[str, JobRecord] = {}
        target_total = initial_total
        observations: list[int] = []

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = (
                initial_payload
                if attempt == 1
                else await self._fetch_page(portal, 1)
            )
            first = self._position_page(payload, expected_page=1)
            observations.append(first["total_count"])
            if first["total_count"] != target_total:
                target_total = first["total_count"]
                union_by_id = {}

            pass_by_id: dict[str, JobRecord] = {}
            total_pages = first["total_pages"]
            partition = "root" if attempt == 1 else f"root-retry-{attempt}"
            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, target_total, False, observations
                current = self._position_page(payload, expected_page=page_number)
                if (
                    current["total_count"] != target_total
                    or current["total_pages"] != total_pages
                ):
                    break
                self.pages_fetched += 1
                self._save_payload(portal.channel, partition, page_number, payload)
                for raw in current["rows"]:
                    record = self.parse_job(raw, portal)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            "Meituan returned conflicting content for job "
                            f"{record.external_id}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._fetch_page(portal, page_number + 1)
            else:
                for external_id, record in pass_by_id.items():
                    previous = union_by_id.get(external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Meituan job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True, observations
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        "Meituan observations exceeded the declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            "Meituan root did not converge after "
            f"{MAX_COLLECTION_ATTEMPTS} attempts: declared={target_total}, "
            f"union={len(union_by_id)}"
        )

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Meituan response data is not an object")
        rows = data.get("list")
        pagination = data.get("page")
        if not isinstance(rows, list) or not isinstance(pagination, dict):
            raise RuntimeError("Meituan response has no list/page payload")
        page_number = pagination.get("pageNo")
        page_size = pagination.get("pageSize")
        total_pages = pagination.get("totalPage")
        total_count = pagination.get("totalCount")
        if page_number != expected_page:
            raise RuntimeError(
                f"Meituan page mismatch: expected={expected_page}, got={page_number!r}"
            )
        if not isinstance(page_size, int) or page_size < 1:
            raise RuntimeError(f"Meituan returned invalid page size: {page_size!r}")
        if not isinstance(total_pages, int) or total_pages < 0:
            raise RuntimeError(f"Meituan returned invalid total pages: {total_pages!r}")
        if not isinstance(total_count, int) or total_count < 0:
            raise RuntimeError(f"Meituan returned invalid total count: {total_count!r}")
        expected_total_pages = (total_count + page_size - 1) // page_size
        if total_pages != expected_total_pages:
            raise RuntimeError(
                "Meituan pagination metadata is inconsistent: "
                f"pages={total_pages}, total={total_count}, size={page_size}"
            )
        if page_size != COLLECTION_PAGE_SIZE:
            raise RuntimeError(
                "Meituan page size changed: "
                f"expected={COLLECTION_PAGE_SIZE}, got={page_size}"
            )
        expected_rows = min(
            page_size,
            max(total_count - (expected_page - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Meituan page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        if total_count and not rows:
            raise RuntimeError(f"Meituan returned empty non-terminal page {expected_page}")
        return {
            "rows": rows,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_count": total_count,
        }

    async def _rate_limit(self) -> None:
        delay = self.settings.meituan_request_delay_seconds - (
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
    def parse_job(raw: dict[str, Any], portal: PortalConfig) -> JobRecord:
        external_id = _required_string(raw, "jobUnionId", "Meituan job id")
        title = _required_string(raw, "name", f"Meituan job title ({external_id})")
        description = _required_string(raw, "jobDuty", f"Meituan job duty ({external_id})")
        requirements = _required_string(
            raw,
            "jobRequirement",
            f"Meituan job requirement ({external_id})",
        )
        employment_type_id = _required_string(raw, "jobType", f"Meituan job type ({external_id})")
        locations = _locations(raw.get("cityList"), external_id)
        categories = _categories(raw)
        department_code, department_name = _first_named_value(raw.get("department"))

        return JobRecord(
            source_key="meituan_cn",
            external_id=external_id,
            source_url=f"{portal.page_url}?jobUnionId={external_id}",
            company_name="美团",
            channel=portal.channel,
            employment_type_id=employment_type_id,
            employment_type_name=portal.employment_type_name,
            recruitment_project_id=_optional_string(raw.get("projectId")),
            recruitment_project_name=_optional_string(raw.get("projectName")),
            title=title,
            description=description,
            requirements=requirements,
            published_at=_timestamp_ms(raw.get("firstPostTime"), "firstPostTime"),
            source_updated_at=_timestamp_ms(raw.get("refreshTime"), "refreshTime"),
            source_status=_optional_string(raw.get("jobStatus")),
            department_code=department_code,
            department_name=department_name,
            locations=locations,
            categories=categories,
            source_payload=raw,
        )


def _required_string(raw: dict[str, Any], key: str, label: str) -> str:
    value = _optional_string(raw.get(key))
    if value is None:
        raise ValueError(f"{label} is missing")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Meituan {field} is not a numeric timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    if not isinstance(value, list):
        raise ValueError(f"Meituan job has no city list: {external_id}")
    locations: list[LocationRecord] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Meituan city item is not an object: {external_id}")
        name = _optional_string(item.get("name"))
        if name is None:
            continue
        code = _optional_string(item.get("code")) or f"city:{name}"
        if code in seen:
            continue
        seen.add(code)
        locations.append(LocationRecord(code=code, name=name))
    if not locations:
        raise ValueError(f"Meituan job has no work location: {external_id}")
    return locations


def _categories(raw: dict[str, Any]) -> list[SourceCategoryRecord]:
    family = _optional_string(raw.get("jobFamily"))
    group = _optional_string(raw.get("jobFamilyGroup"))
    if group is not None and family is not None:
        return [
            SourceCategoryRecord(
                external_id=f"jobFamilyGroup:{family}:{group}",
                name=group,
                parent_external_id=f"jobFamily:{family}",
                parent_name=family,
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ]
    if family is not None:
        return [
            SourceCategoryRecord(
                external_id=f"jobFamily:{family}",
                name=family,
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ]
    return []


def _first_named_value(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, list):
        return None, None
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _optional_string(item.get("name"))
        if name is not None:
            return _optional_string(item.get("code")), name
    return None, None
