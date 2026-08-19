"""Alibaba Group campus recruitment connector.

The portal is a public, browser-rendered page.  This connector only consumes
the JSON responses emitted by that page; it does not recreate request signing
or call authenticated application endpoints.
"""

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.alibaba_common import (
    canonical_position_url,
    coded_label,
    number_range,
    string_list,
    time_range,
    timestamp_ms,
    unique_strings,
)
from job_market.connectors.browser_json import (
    BrowserResponseUnavailableError,
    JsonResponseQueue,
    drain_json_responses,
    enqueue_json_response,
    next_json_payload,
)
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

API_CAP = 10_000
UI_PAGE_SIZE = 10
RESPONSE_TIMEOUT_SECONDS = 30
BATCH_OPEN_ATTEMPTS = 3
POSITION_PAGE_URL = "https://campus-talent.alibaba.com/campus/position"
POSITION_URL = "https://campus-talent.alibaba.com/campus/position/{external_id}?deptCodes="
PORTAL_CHANNEL = "campus_group_official_site"


@dataclass(frozen=True)
class RecruitmentBatch:
    id: int
    name: str
    kind: str


@dataclass(frozen=True)
class AlibabaSourceCatalog:
    category_codes_by_name: dict[str, str]
    business_units_by_code: dict[str, str]


class AlibabaConnector:
    """Collect Alibaba's unified public campus position list."""

    source_key = "alibaba_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._batch_responses: JsonResponseQueue = asyncio.Queue()
        self._condition_responses: JsonResponseQueue = asyncio.Queue()
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.CAMPUS:
            raise ValueError("Alibaba connector currently supports only the campus channel")

        batches = await self._discover_batches()
        jobs_by_id: dict[str, JobRecord] = {}
        partition_counts: dict[str, int] = {}
        complete = True

        for batch in batches:
            total, payload, catalog, condition_payload = await self._open_batch(batch)
            partition = f"batch-{batch.id}-{batch.name}"
            partition_counts[partition] = total
            if total >= API_CAP:
                raise RuntimeError(
                    f"Alibaba batch {batch.name!r} reached the {API_CAP} API cap; "
                    "add a source-specific partition before collecting it."
                )
            if self.raw_store is not None:
                self.snapshots.append(
                    self.raw_store.save(
                        channel=channel,
                        partition=f"{partition}-conditions",
                        offset=0,
                        payload=condition_payload,
                    )
                )

            offset = 0
            while True:
                if max_pages is not None and self.pages_fetched >= max_pages:
                    complete = False
                    break

                self.pages_fetched += 1
                if self.raw_store is not None:
                    self.snapshots.append(
                        self.raw_store.save(
                            channel=channel,
                            partition=partition,
                            offset=offset,
                            payload=payload,
                        )
                    )

                content = self._position_content(payload, partition, offset)
                rows = content.get("datas")
                if not isinstance(rows, list):
                    raise RuntimeError(f"Alibaba position response has no datas: {partition}")
                if not rows and offset < total:
                    raise RuntimeError(
                        f"Alibaba returned an empty non-terminal page: {partition} offset={offset}"
                    )

                for raw in rows:
                    record = self.parse_job(raw, catalog)
                    previous = jobs_by_id.get(record.external_id)
                    if previous is not None:
                        if previous.content_hash() != record.content_hash():
                            raise RuntimeError(
                                "Alibaba returned conflicting content for position "
                                f"{record.external_id}"
                            )
                        continue
                    jobs_by_id[record.external_id] = record

                offset += len(rows)
                if offset >= total or not rows:
                    break
                payload = await self._next_page(batch, offset)

            if not complete:
                break

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _discover_batches(self) -> list[RecruitmentBatch]:
        self._drain_queues()
        await self.page.goto(POSITION_PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        payload = await self._next_payload(self._batch_responses, "batch list")
        return self._parse_batches(payload)

    @staticmethod
    def _parse_batches(payload: dict[str, Any]) -> list[RecruitmentBatch]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise RuntimeError("Alibaba batch response has no content")

        batches: list[RecruitmentBatch] = []
        seen_ids: set[int] = set()
        for kind in ("graduate", "internship", "topTalentPlan"):
            items = content.get(kind) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    batch_id = int(item["id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"Alibaba batch has invalid id: {item!r}") from exc
                if batch_id in seen_ids:
                    continue
                name = str(item.get("name") or batch_id)
                batches.append(RecruitmentBatch(batch_id, name, kind))
                seen_ids.add(batch_id)
        if not batches:
            raise RuntimeError("Alibaba exposed no active campus recruitment batches")
        return batches

    async def _open_batch(
        self,
        batch: RecruitmentBatch,
    ) -> tuple[int, dict[str, Any], AlibabaSourceCatalog, dict[str, Any]]:
        for attempt in range(1, BATCH_OPEN_ATTEMPTS + 1):
            try:
                return await self._open_batch_once(batch)
            except BrowserResponseUnavailableError as exc:
                if attempt == BATCH_OPEN_ATTEMPTS:
                    raise BrowserResponseUnavailableError(
                        f"Alibaba batch {batch.id} did not load after "
                        f"{BATCH_OPEN_ATTEMPTS} attempts"
                    ) from exc
        raise AssertionError("unreachable")

    async def _open_batch_once(
        self,
        batch: RecruitmentBatch,
    ) -> tuple[int, dict[str, Any], AlibabaSourceCatalog, dict[str, Any]]:
        await self._rate_limit()
        self._drain_queue(self._condition_responses)
        self._drain_queue(self._position_responses)
        await self.page.goto(
            f"{POSITION_PAGE_URL}?batchId={batch.id}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        condition_payload = await self._next_payload(
            self._condition_responses,
            f"conditions:{batch.id}",
        )
        catalog = self._parse_source_catalog(condition_payload)
        payload = await self._next_payload(self._position_responses, f"positions:{batch.id}:1")
        content = self._position_content(payload, f"batch-{batch.id}", 0)
        total = content.get("totalCount")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError(
                f"Alibaba returned invalid totalCount for batch {batch.id}: {total!r}"
            )
        return total, payload, catalog, condition_payload

    async def _next_page(self, batch: RecruitmentBatch, offset: int) -> dict[str, Any]:
        await self._rate_limit()
        self._drain_queue(self._position_responses)
        next_button = self.page.locator("button[aria-label^='下一页']")
        await next_button.wait_for(state="visible", timeout=RESPONSE_TIMEOUT_SECONDS * 1000)
        aria_label = await next_button.get_attribute("aria-label")
        if aria_label is None or "disabled" in (await next_button.get_attribute("class") or ""):
            raise RuntimeError(
                f"Alibaba pagination ended before declared count: batch={batch.id} offset={offset}"
            )
        await next_button.click()
        return await self._next_payload(
            self._position_responses,
            f"positions:{batch.id}:{offset // UI_PAGE_SIZE + 1}",
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        queue: JsonResponseQueue | None = None
        if "/searchCondition/listBatch" in response.url:
            queue = self._batch_responses
        elif "/searchCondition/list" in response.url:
            queue = self._condition_responses
        elif "/position/search" in response.url:
            queue = self._position_responses
        if queue is not None:
            enqueue_json_response(queue, response)

    async def _next_payload(
        self,
        queue: JsonResponseQueue,
        operation: str,
    ) -> dict[str, Any]:
        payload = await next_json_payload(
            queue,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Alibaba response: {operation}",
        )
        if payload.get("success") is not True or not isinstance(payload.get("content"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Alibaba response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _rate_limit(self) -> None:
        delay = self.settings.alibaba_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _drain_queues(self) -> None:
        self._drain_queue(self._batch_responses)
        self._drain_queue(self._condition_responses)
        self._drain_queue(self._position_responses)

    @staticmethod
    def _drain_queue(queue: JsonResponseQueue) -> None:
        drain_json_responses(queue)

    @staticmethod
    def _position_content(
        payload: dict[str, Any],
        partition: str,
        offset: int,
    ) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise RuntimeError(f"Alibaba position response has no content: {partition}")
        current_page = content.get("currentPage")
        if current_page is not None and current_page != offset // UI_PAGE_SIZE + 1:
            raise RuntimeError(
                f"Alibaba page mismatch for {partition}: expected {offset // UI_PAGE_SIZE + 1}, "
                f"got {current_page}"
            )
        return content

    @staticmethod
    def _parse_source_catalog(payload: dict[str, Any]) -> AlibabaSourceCatalog:
        content = payload.get("content")
        search_items = content.get("searchItems") if isinstance(content, dict) else None
        if not isinstance(search_items, list):
            raise RuntimeError("Alibaba condition response has no searchItems")

        category_codes_by_name: dict[str, str] = {}
        business_units_by_code: dict[str, str] = {}
        for section in search_items:
            if not isinstance(section, dict) or not isinstance(section.get("items"), list):
                continue
            if section.get("type") == "category":
                for item in section["items"]:
                    code, name = _catalog_item(item, "category")
                    previous = category_codes_by_name.get(name)
                    if previous is not None and previous != code:
                        raise RuntimeError(
                            f"Alibaba category name maps to conflicting codes: {name!r}"
                        )
                    category_codes_by_name[name] = code
            elif section.get("type") == "customDept":
                for item in _walk_catalog_items(section["items"]):
                    code, name = _catalog_item(item, "customDept")
                    previous = business_units_by_code.get(code)
                    if previous is not None and previous != name:
                        raise RuntimeError(
                            f"Alibaba business-unit code maps to conflicting names: {code!r}"
                        )
                    business_units_by_code[code] = name

        if not category_codes_by_name:
            raise RuntimeError("Alibaba condition response has no category dictionary")
        if not business_units_by_code:
            raise RuntimeError("Alibaba condition response has no business-unit dictionary")
        return AlibabaSourceCatalog(category_codes_by_name, business_units_by_code)

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        catalog: AlibabaSourceCatalog,
    ) -> JobRecord:
        external_id = str(raw.get("id") or "").strip()
        title = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        requirements = str(raw.get("requirement") or "").strip()
        if not external_id or not title or not description or not requirements:
            raise ValueError(f"Alibaba job is missing a required fact: {raw.get('id')!r}")

        category_name = str(raw.get("categoryName") or "").strip()
        categories: list[SourceCategoryRecord] = []
        if category_name:
            category_id = catalog.category_codes_by_name.get(category_name)
            if category_id is None:
                raise ValueError(
                    f"Alibaba job category is absent from official conditions: {category_name!r}"
                )
            categories.append(
                SourceCategoryRecord(
                    external_id=category_id,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            )

        category_type = str(raw.get("categoryType") or "").strip()
        if not category_type:
            raise ValueError(f"Alibaba job has no categoryType: {external_id}")
        if category_type == "freshman":
            employment_name = "应届生"
        elif category_type == "project":
            employment_name = "实习生"
        else:
            employment_name = category_type

        locations: list[LocationRecord] = []
        seen_locations: set[str] = set()
        for item in raw.get("workLocations") or []:
            name = str(item).strip()
            if not name or name in seen_locations:
                continue
            seen_locations.add(name)
            locations.append(LocationRecord(code=f"city:{name}", name=name))
        if not locations:
            raise ValueError(f"Alibaba job has no work location: {external_id}")

        business_units = _business_units(raw, catalog.business_units_by_code)
        graduation_start_at, graduation_end_at = time_range(
            raw.get("graduationTime"),
            "graduationTime",
        )
        degree_code, degree_name = coded_label(raw.get("degree"), "degree")
        department_code, department_name = coded_label(
            raw.get("department"),
            "department",
            string_is_name=True,
        )
        experience_min, experience_max = number_range(
            raw.get("experience"),
            "experience",
        )

        return JobRecord(
            source_key="alibaba_cn",
            external_id=external_id,
            external_code=str(raw["code"]).strip() if raw.get("code") else None,
            source_url=canonical_position_url(
                raw.get("positionUrl"),
                POSITION_URL.format(external_id=external_id),
                POSITION_PAGE_URL,
            ),
            company_name="阿里巴巴集团",
            channel=Channel.CAMPUS,
            employment_type_id=category_type,
            employment_type_name=employment_name,
            recruitment_project_id=(str(raw["batchId"]) if raw.get("batchId") else None),
            recruitment_project_name=(str(raw["batchName"]) if raw.get("batchName") else None),
            title=title,
            description=description,
            requirements=requirements,
            published_at=timestamp_ms(raw.get("publishTime"), "publishTime"),
            source_updated_at=timestamp_ms(raw.get("modifyTime"), "modifyTime"),
            source_status=(str(raw["status"]).strip() if raw.get("status") is not None else None),
            degree_code=degree_code,
            degree_name=degree_name,
            experience_min_years=experience_min,
            experience_max_years=experience_max,
            graduation_start_at=graduation_start_at,
            graduation_end_at=graduation_end_at,
            department_code=department_code,
            department_name=department_name,
            interview_location_names=unique_strings(
                raw.get("interviewLocations"),
                "interviewLocations",
            ),
            is_hot=None,
            locations=locations,
            categories=categories,
            business_units=business_units,
            source_payload=raw,
        )


def _business_units(
    raw: dict[str, Any],
    official_names_by_code: dict[str, str],
) -> list[BusinessUnitRecord]:
    """Resolve circle codes through the official condition dictionary.

    ``circleCodeList`` and ``circleNames`` are not parallel arrays: the live
    portal returns them in different orders. The code dictionary is
    authoritative; the names array is only used as an integrity check.
    """

    codes = string_list(raw.get("circleCodeList"), "circleCodeList")
    names = string_list(raw.get("circleNames"), "circleNames")
    if not codes and not names:
        return []
    resolved_names: list[str] = []
    for code in codes:
        name = official_names_by_code.get(code)
        if name is None:
            raise ValueError(
                f"Alibaba business-unit code is absent from official conditions: {code!r}"
            )
        resolved_names.append(name)
    if Counter(resolved_names) != Counter(names):
        raise ValueError(
            "Alibaba circleNames do not match circleCodeList through official conditions"
        )
    return [
        BusinessUnitRecord(code=code, name=official_names_by_code[code])
        for code in dict.fromkeys(codes)
    ]


def _walk_catalog_items(items: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    pending = list(items)
    while pending:
        item = pending.pop(0)
        if not isinstance(item, dict):
            raise RuntimeError(f"Alibaba condition item is not an object: {item!r}")
        flattened.append(item)
        children = item.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise RuntimeError("Alibaba condition item children must be a list")
            pending.extend(children)
    return flattened


def _catalog_item(item: Any, section: str) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise RuntimeError(f"Alibaba {section} condition item is not an object")
    code = str(item.get("value") or "").strip()
    name = str(item.get("label") or "").strip()
    if not code or not name:
        raise RuntimeError(f"Alibaba {section} condition item has no code or name")
    return code, name
