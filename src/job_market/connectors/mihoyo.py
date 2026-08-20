"""miHoYo public experienced, campus, and internship connector."""

import asyncio
import json
from dataclasses import dataclass
from math import ceil
from typing import Any

from playwright.async_api import Page

from job_market.config import Settings
from job_market.connectors.retry import retry_async
from job_market.raw_store import RawStore
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
    SourceCategoryRecord,
)

LIST_PAGE_SIZE = 100
DETAIL_CONCURRENCY = 3
DETAIL_BATCH_SIZE = 100
REQUEST_TIMEOUT_MS = 60_000
DETAIL_REQUEST_TIMEOUT_MS = 30_000
CONTEXT_URL = "https://jobs.mihoyo.com/robots.txt"
LIST_ENDPOINT = "https://ats.openout.mihoyo.com/ats-portal/v1/job/list"
DETAIL_ENDPOINT = "https://ats.openout.mihoyo.com/ats-portal/v1/job/info"


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    hire_type: int
    project_ids: tuple[int, ...]
    list_url: str
    detail_path: str

    def detail_url(self, external_id: str) -> str:
        return f"https://jobs.mihoyo.com/#/{self.detail_path}/{external_id}"


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        hire_type=0,
        project_ids=(),
        list_url="https://jobs.mihoyo.com/#/position",
        detail_path="position",
    ),
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        hire_type=1,
        project_ids=(13,),
        list_url="https://jobs.mihoyo.com/#/campus/position",
        detail_path="campus/position",
    ),
    Channel.INTERNSHIP: PortalConfig(
        channel=Channel.INTERNSHIP,
        hire_type=1,
        project_ids=(4,),
        list_url="https://jobs.mihoyo.com/#/campus/position",
        detail_path="campus/position",
    ),
}


class MihoyoConnector:
    """Collect complete public lists and the detail response for every job."""

    source_key = "mihoyo_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._request_slot = asyncio.Lock()

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("miHoYo connector does not support this channel")

        await self.page.goto(
            CONTEXT_URL,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT_MS,
        )
        initial_rows, initial_total, initial_complete = await self._collect_list_pass(
            portal,
            partition="list-initial",
            max_pages=max_pages,
        )
        detail_results = await self._collect_details(portal, list(initial_rows.values()))
        jobs = [record for record, _ in detail_results]
        self._save_detail_batches(channel, initial_rows, detail_results)

        partition_counts = {
            "all": initial_total,
            "list-initial": len(initial_rows),
            "details": len(jobs),
            "collected-unique": len(jobs),
        }
        if not initial_complete:
            return self._result(channel, jobs, partition_counts, complete=False)

        final_rows, final_total, final_complete = await self._collect_list_pass(
            portal,
            partition="list-final",
            max_pages=max_pages,
        )
        partition_counts["list-final"] = len(final_rows)
        issues: list[CollectionIssue] = []
        complete = final_complete
        if final_complete and not _same_list_observation(
            initial_rows,
            initial_total,
            final_rows,
            final_total,
        ):
            complete = False
            issues.append(
                CollectionIssue(
                    scope="source",
                    error_type="ListChangedDuringCollection",
                    message=(
                        "miHoYo list changed while job details were collected: "
                        f"initial_total={initial_total}, final_total={final_total}, "
                        f"initial_unique={len(initial_rows)}, final_unique={len(final_rows)}"
                    ),
                )
            )
        return self._result(
            channel,
            jobs,
            partition_counts,
            complete=complete,
            issues=issues,
        )

    async def _collect_list_pass(
        self,
        portal: PortalConfig,
        *,
        partition: str,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], int, bool]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        total: int | None = None
        total_pages = 1
        page_number = 1
        while page_number <= total_pages:
            if max_pages is not None and self.pages_fetched >= max_pages:
                return rows_by_id, total or 0, False
            payload = await self._fetch_list_page(portal, page_number)
            current = self._list_page(payload, expected_page=page_number)
            if total is None:
                total = current["total"]
                total_pages = max(1, ceil(total / LIST_PAGE_SIZE))
            elif current["total"] != total:
                raise RuntimeError(
                    "miHoYo total changed within a list pass: "
                    f"expected={total}, got={current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(portal.channel, partition, page_number - 1, payload)
            for row in current["rows"]:
                external_id = _required(row, "id", "miHoYo list job id")
                if external_id in rows_by_id:
                    raise RuntimeError(f"miHoYo list repeated job {external_id}")
                rows_by_id[external_id] = row
            page_number += 1

        if total is None:
            raise AssertionError("miHoYo list pass returned no pagination metadata")
        if len(rows_by_id) != total:
            raise RuntimeError(
                "miHoYo pagination count mismatch: "
                f"declared={total}, unique={len(rows_by_id)}"
            )
        return rows_by_id, total, True

    async def _fetch_list_page(
        self,
        portal: PortalConfig,
        page_number: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "pageNo": page_number,
            "pageSize": LIST_PAGE_SIZE,
            "channelDetailIds": [1],
            "hireType": portal.hire_type,
        }
        if portal.project_ids:
            body["projectIds"] = list(portal.project_ids)
        return await self._post_json(
            LIST_ENDPOINT,
            body,
            referer=portal.list_url,
            operation=f"list:{portal.channel.value}:{page_number}",
        )

    async def _collect_details(
        self,
        portal: PortalConfig,
        list_rows: list[dict[str, Any]],
    ) -> list[tuple[JobRecord, dict[str, Any]]]:
        if not list_rows:
            return []

        pending: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        for item in enumerate(list_rows):
            pending.put_nowait(item)
        worker_count = min(DETAIL_CONCURRENCY, len(list_rows))
        for _ in range(worker_count):
            pending.put_nowait(None)
        results: list[tuple[JobRecord, dict[str, Any]] | None] = [None] * len(list_rows)

        async def worker() -> None:
            while True:
                item = await pending.get()
                if item is None:
                    return
                index, list_row = item
                external_id = _required(list_row, "id", "miHoYo list job id")
                payload = await self._post_json(
                    DETAIL_ENDPOINT,
                    {
                        "id": external_id,
                        "channelDetailIds": [1],
                        "hireType": portal.hire_type,
                    },
                    referer=portal.detail_url(external_id),
                    operation=f"detail:{external_id}",
                    timeout_ms=DETAIL_REQUEST_TIMEOUT_MS,
                )
                detail = payload["data"]
                record = self.parse_job(list_row, detail, portal)
                results[index] = (record, payload)

        await asyncio.gather(*(worker() for _ in range(worker_count)))
        if any(result is None for result in results):
            raise RuntimeError("miHoYo detail collection finished with missing results")
        return [result for result in results if result is not None]

    async def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        *,
        referer: str,
        operation: str,
        timeout_ms: int = REQUEST_TIMEOUT_MS,
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            await self._wait_for_request_slot()
            response = await self.page.request.post(
                url,
                data=body,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://jobs.mihoyo.com",
                    "Referer": referer,
                },
                timeout=timeout_ms,
            )
            if response.status != 200:
                raise RuntimeError(
                    f"miHoYo endpoint returned HTTP {response.status} for {operation}"
                )
            payload = await response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("code") != 0
                or payload.get("success") is not True
                or not isinstance(payload.get("data"), dict)
            ):
                message = json.dumps(payload, ensure_ascii=False)[:1000]
                raise RuntimeError(f"Invalid miHoYo response for {operation}: {message}")
            return payload

        return await retry_async(
            request,
            source=self.source_key,
            operation_name=operation,
            attempts=3,
            base_delay_seconds=1.0,
        )

    async def _wait_for_request_slot(self) -> None:
        async with self._request_slot:
            loop = asyncio.get_running_loop()
            delay = self.settings.mihoyo_request_delay_seconds - (
                loop.time() - self._last_request_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = loop.time()

    @staticmethod
    def _list_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("miHoYo list response has no data object")
        rows = data.get("list")
        page_number = _response_int(data.get("pageNo"), "pageNo")
        page_size = _response_int(data.get("pageSize"), "pageSize")
        total = _response_int(data.get("total"), "total", allow_zero=True)
        if page_number != expected_page:
            raise RuntimeError(
                f"miHoYo page mismatch: expected={expected_page}, got={page_number}"
            )
        if page_size != LIST_PAGE_SIZE:
            raise RuntimeError(
                f"miHoYo page size mismatch: expected={LIST_PAGE_SIZE}, got={page_size}"
            )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("miHoYo list response contains an invalid job list")
        expected_rows = min(
            LIST_PAGE_SIZE,
            max(total - (page_number - 1) * LIST_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"miHoYo page {page_number} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(
        list_raw: dict[str, Any],
        detail: dict[str, Any],
        portal: PortalConfig,
    ) -> JobRecord:
        _assert_list_detail_match(list_raw, detail, portal)
        external_id = _required(detail, "id", "miHoYo detail job id")
        title = _required(detail, "title", f"miHoYo job title ({external_id})")
        employment_id = _required(
            detail,
            "jobNatureId",
            f"miHoYo job nature id ({external_id})",
        )
        employment_name = _required(
            detail,
            "jobNature",
            f"miHoYo job nature ({external_id})",
        )
        project_id = _optional(detail.get("projectId"))
        project_name = _optional(detail.get("projectName"))
        if (project_id is None) != (project_name is None):
            raise ValueError(f"miHoYo job {external_id} has an incomplete project")
        if portal.project_ids and project_id not in {
            str(project) for project in portal.project_ids
        }:
            raise ValueError(
                f"miHoYo job {external_id} belongs to unexpected project {project_id!r}"
            )
        category_id = _required(
            detail,
            "competencyTypeId",
            f"miHoYo category id ({external_id})",
        )
        category_name = _required(
            detail,
            "competencyType",
            f"miHoYo category ({external_id})",
        )
        return JobRecord(
            source_key="mihoyo_cn",
            external_id=external_id,
            external_code=_optional(detail.get("code")),
            source_url=portal.detail_url(external_id),
            company_name="米哈游",
            channel=portal.channel,
            employment_type_id=employment_id,
            employment_type_name=employment_name,
            recruitment_project_id=project_id,
            recruitment_project_name=project_name,
            title=title,
            description=_optional(detail.get("description")),
            requirements=_requirements(detail),
            source_status=_optional(detail.get("status")),
            is_hot=_optional_bool(detail.get("hurry"), external_id),
            locations=_locations(detail.get("addressDetailList"), external_id),
            categories=[
                SourceCategoryRecord(
                    external_id=category_id,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload={"list": list_raw, "detail": detail},
        )

    def _save_detail_batches(
        self,
        channel: Channel,
        list_rows: dict[str, dict[str, Any]],
        detail_results: list[tuple[JobRecord, dict[str, Any]]],
    ) -> None:
        for start in range(0, len(detail_results), DETAIL_BATCH_SIZE):
            batch = detail_results[start : start + DETAIL_BATCH_SIZE]
            self._save_payload(
                channel,
                "details",
                start,
                {
                    "items": [
                        {
                            "list": list_rows[record.external_id],
                            "detail": payload,
                        }
                        for record, payload in batch
                    ]
                },
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

    def _result(
        self,
        channel: Channel,
        jobs: list[JobRecord],
        partition_counts: dict[str, int],
        *,
        complete: bool,
        issues: list[CollectionIssue] | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
            issues=issues or [],
        )


def _assert_list_detail_match(
    list_raw: dict[str, Any],
    detail: dict[str, Any],
    portal: PortalConfig,
) -> None:
    external_id = _required(list_raw, "id", "miHoYo list job id")
    detail_id = _required(detail, "id", "miHoYo detail job id")
    if detail_id != external_id:
        raise ValueError(
            f"miHoYo detail response mismatch: requested={external_id}, got={detail_id}"
        )
    hire_type = detail.get("hireType")
    if isinstance(hire_type, bool) or hire_type != portal.hire_type:
        raise ValueError(
            f"miHoYo job {external_id} hire type {hire_type!r} does not match "
            f"channel {portal.channel.value}"
        )
    for field in (
        "title",
        "competencyType",
        "jobNatureId",
        "jobNature",
        "projectName",
        "objectId",
        "objectName",
    ):
        if _optional(list_raw.get(field)) != _optional(detail.get(field)):
            raise ValueError(
                f"miHoYo job {external_id} changed between list and detail field {field}"
            )
    if _address_pairs(list_raw.get("addressDetailList"), external_id) != _address_pairs(
        detail.get("addressDetailList"),
        external_id,
    ):
        raise ValueError(
            f"miHoYo job {external_id} changed between list and detail locations"
        )


def _same_list_observation(
    initial: dict[str, dict[str, Any]],
    initial_total: int,
    final: dict[str, dict[str, Any]],
    final_total: int,
) -> bool:
    if initial_total != final_total or set(initial) != set(final):
        return False
    return all(
        json.dumps(initial[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == json.dumps(final[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key in initial
    )


def _requirements(detail: dict[str, Any]) -> str | None:
    parts: list[str] = []
    requirement = _optional(detail.get("jobRequire"))
    if requirement is not None:
        parts.append(requirement)
    addition = _optional(detail.get("addition"))
    if addition is not None:
        parts.append(f"加分项：\n{addition}")
    instructions = _optional(detail.get("deliveryInstructions"))
    if instructions is not None:
        parts.append(f"投递说明：\n{instructions}")
    return "\n\n".join(parts) or None


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    return [
        LocationRecord(code=code, name=name)
        for code, name in _address_pairs(value, external_id)
    ]


def _address_pairs(value: Any, external_id: str) -> list[tuple[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"miHoYo job {external_id} has no address list")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(f"miHoYo job {external_id} has an invalid address")
        code = _required(raw, "addressId", f"miHoYo address id ({external_id})")
        name = _required(raw, "addressDetail", f"miHoYo address ({external_id})")
        if code in seen:
            raise ValueError(f"miHoYo job {external_id} repeats address {code}")
        seen.add(code)
        result.append((code, name))
    return result


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{label} is missing")
    result = _optional(value)
    if result is None:
        raise ValueError(f"{label} is missing")
    return result


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"miHoYo {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"miHoYo {field} is invalid: {value!r}")
    return value


def _optional_bool(value: Any, external_id: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"miHoYo job {external_id} hurry flag is invalid")
    return value
