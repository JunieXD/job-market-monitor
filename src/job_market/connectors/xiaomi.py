"""Xiaomi public recruitment-project connector."""

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, time
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
    Channel,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
)

RESPONSE_TIMEOUT_SECONDS = 30
POSITION_LIST_URL = "https://hr.xiaomi.com/website/opportunities.html"


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    type_id: int
    employment_type_name: str


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(Channel.EXPERIENCED, 1, "社招"),
    Channel.CAMPUS: PortalConfig(Channel.CAMPUS, 2, "校招"),
    Channel.INTERNSHIP: PortalConfig(Channel.INTERNSHIP, 3, "实习"),
}

TOP_TALENT_PROJECT_ID = "top_talent"
TOP_TALENT_PROJECT_NAME = "顶尖人才"


class XiaomiConnector:
    """Collect Xiaomi jobs by clicking the site's explicit project filters."""

    source_key = "xiaomi_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Xiaomi connector does not support this channel")

        payload = await self._open_portal(portal)
        first = self._page_data(payload, expected_page=1)
        total_pages = first["pages"]
        total_count = first["total"]
        jobs_by_id: dict[str, JobRecord] = {}
        filter_job_ids: set[str] = set()
        excluded_other_type_count = 0
        partition_counts = {"all": total_count}
        complete = True

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            current = self._page_data(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_payload(channel, "all", page_number, payload)
            for raw in current["rows"]:
                external_id, type_id = self._job_identity(raw)
                if external_id in filter_job_ids:
                    raise RuntimeError(
                        f"Xiaomi project filter repeated job {external_id}"
                    )
                filter_job_ids.add(external_id)
                if type_id != portal.type_id:
                    excluded_other_type_count += 1
                    continue
                record = self.parse_job(raw, portal)
                jobs_by_id[record.external_id] = record
            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if complete and len(filter_job_ids) != total_count:
            raise RuntimeError(
                "Xiaomi pagination count mismatch: "
                f"declared={total_count}, unique={len(filter_job_ids)}"
            )
        if excluded_other_type_count:
            partition_counts["excluded-other-employment-types"] = (
                excluded_other_type_count
            )
        partition_counts["selected-employment-type"] = len(jobs_by_id)

        if complete and channel in {Channel.CAMPUS, Channel.INTERNSHIP}:
            complete = await self._collect_top_talent_memberships(
                portal,
                jobs_by_id,
                partition_counts,
                max_pages,
            )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_portal(self, portal: PortalConfig) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        await self.page.goto(
            POSITION_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        # The first response is the unfiltered aggregate used to render the page.
        await self._next_payload("positions:unfiltered")

        return await self._select_project(
            portal.employment_type_name,
            f"positions:{portal.type_id}:1",
        )

    async def _select_project(
        self,
        project_name: str,
        operation: str,
    ) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        project_button = self.page.get_by_role(
            "button",
            name=project_name,
            exact=True,
        )
        if await project_button.count() != 1:
            raise RuntimeError(
                f"Cannot uniquely locate Xiaomi project filter: {project_name}"
            )
        await project_button.click()
        return await self._next_payload(operation)

    async def _collect_top_talent_memberships(
        self,
        portal: PortalConfig,
        jobs_by_id: dict[str, JobRecord],
        partition_counts: dict[str, int],
        max_pages: int | None,
    ) -> bool:
        if max_pages is not None and self.pages_fetched >= max_pages:
            return False
        payload = await self._select_project(
            TOP_TALENT_PROJECT_NAME,
            "positions:top-talent:1",
        )
        first = self._page_data(payload, expected_page=1)
        total_pages = first["pages"]
        total_count = first["total"]
        partition = "project-top-talent"
        partition_counts[partition] = total_count
        seen_ids: set[str] = set()
        selected_ids: set[str] = set()

        for page_number in range(1, total_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                return False
            current = self._page_data(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_payload(portal.channel, partition, page_number, payload)
            for raw in current["rows"]:
                external_id, type_id = self._job_identity(raw)
                if external_id in seen_ids:
                    raise RuntimeError(
                        f"Xiaomi top-talent filter repeated job {external_id}"
                    )
                seen_ids.add(external_id)
                if type_id != portal.type_id:
                    continue
                candidate = self.parse_job(raw, portal)
                selected_ids.add(candidate.external_id)
                stored = jobs_by_id.get(candidate.external_id)
                if stored is None:
                    raise RuntimeError(
                        "Xiaomi top-talent filter returned a job absent from the "
                        f"campus snapshot: {candidate.external_id}"
                    )
                if candidate.content_hash() != stored.content_hash():
                    raise RuntimeError(
                        "Xiaomi job changed while top-talent membership was collected: "
                        f"{candidate.external_id}"
                    )
                jobs_by_id[candidate.external_id] = stored.model_copy(
                    update={
                        "recruitment_project_id": TOP_TALENT_PROJECT_ID,
                        "recruitment_project_name": TOP_TALENT_PROJECT_NAME,
                    }
                )
            if page_number < total_pages:
                payload = await self._next_page(page_number + 1)

        if len(seen_ids) != total_count:
            raise RuntimeError(
                "Xiaomi top-talent count mismatch: "
                f"declared={total_count}, unique={len(seen_ids)}"
            )
        partition_counts[f"{partition}-selected-employment-type"] = len(selected_ids)
        return True

    @staticmethod
    def _job_identity(raw: dict[str, Any]) -> tuple[str, int]:
        external_id = _required(raw, "jobPostId", "Xiaomi job id")
        type_value = raw.get("type")
        if isinstance(type_value, bool) or type_value not in {
            item.type_id for item in PORTALS.values()
        }:
            raise ValueError(
                f"Xiaomi job {external_id} has unknown type {type_value!r}"
            )
        return external_id, type_value

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.get_by_role("button", name="下一页", exact=True)
        if await next_button.count() != 1:
            raise RuntimeError("Xiaomi pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(f"Xiaomi pagination ended before page {page_number}")
        await next_button.click()
        return await self._next_payload(f"positions:{page_number}")

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Xiaomi response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Xiaomi response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _page_data(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Xiaomi response data is not an object")
        rows = data.get("list")
        page_number = data.get("pageNum")
        page_size = data.get("pageSize")
        pages = data.get("pageTotal")
        total = data.get("total")
        if not isinstance(rows, list):
            raise RuntimeError("Xiaomi response has no job list")
        if page_number != expected_page:
            raise RuntimeError(
                f"Xiaomi page mismatch: expected={expected_page}, got={page_number!r}"
            )
        if not isinstance(page_size, int) or page_size < 1:
            raise RuntimeError(f"Xiaomi returned invalid page size: {page_size!r}")
        if not isinstance(pages, int) or pages < 0:
            raise RuntimeError(f"Xiaomi returned invalid page count: {pages!r}")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError(f"Xiaomi returned invalid total: {total!r}")
        if pages != (total + page_size - 1) // page_size:
            raise RuntimeError(
                "Xiaomi pagination metadata is inconsistent: "
                f"pages={pages}, total={total}, size={page_size}"
            )
        if total and not rows:
            raise RuntimeError(f"Xiaomi returned an empty non-terminal page {expected_page}")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"Xiaomi page {expected_page} contains a non-object row")
        return {"rows": rows, "pages": pages, "total": total}

    def _record_response(self, response: Response) -> None:
        if response.status == 200 and "/website/api/agent/searchJobPage" in response.url:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.xiaomi_request_delay_seconds - (
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
        external_id = _required(raw, "jobPostId", "Xiaomi job id")
        title = _required(raw, "title", f"Xiaomi job title ({external_id})")
        description = _optional(raw.get("description"))
        requirements = _optional(raw.get("requirement"))
        type_value = raw.get("type")
        if type_value != portal.type_id:
            raise ValueError(
                f"Xiaomi job {external_id} has type {type_value!r}; "
                f"expected {portal.type_id}"
            )
        source_url = _required(raw, "url", f"Xiaomi source URL ({external_id})")
        locations = _locations(raw.get("cityZhNames"), external_id)
        return JobRecord(
            source_key="xiaomi_cn",
            external_id=external_id,
            external_code=_optional(raw.get("larkJobCode")),
            source_url=source_url,
            company_name="小米",
            channel=portal.channel,
            employment_type_id=str(portal.type_id),
            employment_type_name=portal.employment_type_name,
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_date(raw.get("publishTime")),
            department_name=_optional(raw.get("levelOneDeptName")),
            locations=locations,
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


def _source_date(value: Any) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Xiaomi publishTime is not an ISO date: {text!r}") from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    if not isinstance(value, list):
        return []
    locations: list[LocationRecord] = []
    seen: set[str] = set()
    for item in value:
        name = _optional(item)
        if name is None or name in seen:
            continue
        seen.add(name)
        locations.append(LocationRecord(code=f"city:{name}", name=name))
    return locations
