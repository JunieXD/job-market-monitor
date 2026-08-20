"""Hupu public experienced and internship recruitment connector."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page

from job_market.config import Settings
from job_market.connectors.retry import retry_async
from job_market.raw_store import RawStore
from job_market.schemas import (
    Channel,
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    RawSnapshotRecord,
)

PAGE_SIZE = 1000
MAX_STABILITY_PASSES = 3
REQUEST_TIMEOUT_MS = 60_000
CONTEXT_URL = "https://hupu.zhiye.com/robots.txt"
POSITION_ENDPOINT = "https://hupu.zhiye.com/api/Jobad/GetJobAdPageList"
DISPLAY_FIELDS = [
    "Category",
    "Kind",
    "LocId",
    "PostDate",
    "WorkWeChatQrCode",
]


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    category_id: str
    list_url: str


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        Channel.EXPERIENCED,
        "1",
        "https://hupu.zhiye.com/social/jobs",
    ),
    Channel.INTERNSHIP: PortalConfig(
        Channel.INTERNSHIP,
        "3",
        "https://hupu.zhiye.com/intern/jobs",
    ),
}


class HupuConnector:
    """Collect Hupu's full-text list with a large, source-supported page size."""

    source_key = "hupu_cn"

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
            raise ValueError("Hupu connector supports experienced and internship jobs")

        await self.page.goto(
            CONTEXT_URL,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT_MS,
        )
        rows, total, complete, issues = await self._collect_stable(portal, max_pages)
        jobs = [self.parse_job(raw, portal) for raw in rows.values()]
        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts={
                "all": total,
                "collected-unique": len(jobs),
            },
            pages_fetched=self.pages_fetched,
            complete=complete,
            issues=issues,
        )

    async def _collect_stable(
        self,
        portal: PortalConfig,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], int, bool, list[CollectionIssue]]:
        rows, total, complete = await self._collect_pass(
            portal,
            partition="root-initial",
            max_pages=max_pages,
        )
        if not complete or total <= PAGE_SIZE:
            return rows, total, complete, []

        previous_rows = rows
        previous_total = total
        for attempt in range(2, MAX_STABILITY_PASSES + 1):
            current_rows, current_total, current_complete = await self._collect_pass(
                portal,
                partition=f"root-stability-{attempt}",
                max_pages=max_pages,
            )
            if not current_complete:
                effective_total = current_total if current_rows else previous_total
                return current_rows or previous_rows, effective_total, False, []
            if _same_observation(
                previous_rows,
                previous_total,
                current_rows,
                current_total,
            ):
                return current_rows, current_total, True, []
            previous_rows = current_rows
            previous_total = current_total

        issue = CollectionIssue(
            scope="source",
            error_type="ListChangedDuringPagination",
            message=(
                "Hupu list did not produce two consecutive stable passes after "
                f"{MAX_STABILITY_PASSES} attempts"
            ),
        )
        return previous_rows, previous_total, False, [issue]

    async def _collect_pass(
        self,
        portal: PortalConfig,
        *,
        partition: str,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], int, bool]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        total: int | None = None
        total_pages = 1
        page_index = 0
        while page_index < total_pages:
            if max_pages is not None and self.pages_fetched >= max_pages:
                return rows_by_id, total or 0, False
            payload = await self._fetch_page(portal, page_index)
            current = self._position_page(payload, expected_page_index=page_index)
            if total is None:
                total = current["total"]
                total_pages = max(1, ceil(total / PAGE_SIZE))
            elif current["total"] != total:
                raise RuntimeError(
                    "Hupu total changed within a page pass: "
                    f"expected={total}, got={current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(portal.channel, partition, page_index, payload)
            for raw in current["rows"]:
                external_id = _required(raw, "Id", "Hupu job id")
                if external_id in rows_by_id:
                    raise RuntimeError(f"Hupu page pass repeated job {external_id}")
                rows_by_id[external_id] = raw
            page_index += 1

        if total is None:
            raise AssertionError("Hupu page pass returned no pagination metadata")
        if len(rows_by_id) != total:
            raise RuntimeError(
                f"Hupu pagination count mismatch: declared={total}, "
                f"unique={len(rows_by_id)}"
            )
        return rows_by_id, total, True

    async def _fetch_page(
        self,
        portal: PortalConfig,
        page_index: int,
    ) -> dict[str, Any]:
        async def request() -> dict[str, Any]:
            await self._rate_limit()
            response = await self.page.request.post(
                POSITION_ENDPOINT,
                data={
                    "PageIndex": page_index,
                    "PageSize": PAGE_SIZE,
                    "Category": [portal.category_id],
                    "KeyWords": "",
                    "SpecialType": 0,
                    "PortalId": "",
                    "DisplayFields": DISPLAY_FIELDS,
                },
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://hupu.zhiye.com",
                    "Referer": portal.list_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=REQUEST_TIMEOUT_MS,
            )
            self._last_request_at = asyncio.get_running_loop().time()
            if response.status != 200:
                raise RuntimeError(
                    f"Hupu endpoint returned HTTP {response.status} for page {page_index}"
                )
            payload = await response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("Code") != 200
                or not isinstance(payload.get("Data"), list)
            ):
                message = json.dumps(payload, ensure_ascii=False)[:1000]
                raise RuntimeError(f"Invalid Hupu response for page {page_index}: {message}")
            return payload

        return await retry_async(
            request,
            source=self.source_key,
            operation_name=f"list:{portal.channel.value}:{page_index}",
            attempts=3,
            base_delay_seconds=1.0,
        )

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page_index: int,
    ) -> dict[str, Any]:
        total = _response_int(payload.get("Count"), "Count", allow_zero=True)
        rows = payload.get("Data")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Hupu response has invalid Data")
        expected_rows = min(
            PAGE_SIZE,
            max(total - expected_page_index * PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Hupu page {expected_page_index} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any], portal: PortalConfig) -> JobRecord:
        external_id = _required(raw, "Id", "Hupu job id")
        title = _required(raw, "JobAdName", f"Hupu job title ({external_id})")
        category_id = _required(
            raw,
            "CategoryId",
            f"Hupu recruitment category id ({external_id})",
        )
        if category_id != portal.category_id:
            raise ValueError(
                f"Hupu job {external_id} category {category_id!r} does not match "
                f"channel {portal.channel.value}"
            )
        employment_name = _required(
            raw,
            "Kind",
            f"Hupu employment type ({external_id})",
        )
        published_at = _timestamp_ms(raw.get("PostDateInt"), "PostDateInt")
        published_text = _source_datetime(raw.get("PostDate"), "PostDate")
        if (
            published_at is not None
            and published_text is not None
            and published_at != published_text
        ):
            raise ValueError(
                f"Hupu job {external_id} has conflicting PostDate and PostDateInt"
            )
        return JobRecord(
            source_key="hupu_cn",
            external_id=external_id,
            external_code=_optional(raw.get("JobAdId")),
            source_url=portal.list_url,
            company_name="虎扑",
            channel=portal.channel,
            employment_type_id=f"kind:{employment_name}",
            employment_type_name=employment_name,
            title=title,
            description=_optional(raw.get("Duty")),
            requirements=_optional(raw.get("Require")),
            published_at=published_at,
            source_updated_at=_source_datetime(raw.get("ChangeDate"), "ChangeDate"),
            source_status=_optional(raw.get("Status")),
            department_code=_optional(raw.get("OrgId")),
            department_name=_optional(raw.get("Org")),
            is_hot=_optional_bool(raw.get("Channel4IsHot"), "Channel4IsHot"),
            locations=[
                LocationRecord(code=f"name:{name}", name=name)
                for name in _unique_strings(raw.get("LocNames"), "LocNames")
            ],
            source_payload=raw,
        )

    async def _rate_limit(self) -> None:
        delay = self.settings.hupu_request_delay_seconds - (
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


def _same_observation(
    left: dict[str, dict[str, Any]],
    left_total: int,
    right: dict[str, dict[str, Any]],
    right_total: int,
) -> bool:
    if left_total != right_total or set(left) != set(right):
        return False
    return all(
        json.dumps(left[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == json.dumps(right[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key in left
    )


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
        raise RuntimeError(f"Hupu {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Hupu {field} is invalid: {value!r}")
    return value


def _unique_strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Hupu {field} must be a list")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _timestamp_ms(value: Any, field: str) -> datetime | None:
    if value in (None, 0):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Hupu {field} is not a numeric timestamp: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=ZoneInfo("Asia/Shanghai"))


def _source_datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None or text.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Hupu {field} is invalid: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"Hupu {field} is not boolean: {value!r}")
    return value
