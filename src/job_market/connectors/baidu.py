"""Baidu's anonymous campus and internship recruitment endpoint."""

import asyncio
import json
import re
from datetime import datetime
from math import ceil
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

LIST_URL = "https://talent.baidu.com/jobs/list"
API_URL = "https://talent.baidu.com/httservice/getPostListNew"
COLLECTOR_USER_AGENT = (
    "job-market-monitor/0.1 (+https://github.com/job-market-monitor/job-market-monitor)"
)
PAGE_SIZE = 10
MAX_COLLECTION_ATTEMPTS = 5
RESPONSE_TIMEOUT_SECONDS = 30
SHANGHAI = ZoneInfo("Asia/Shanghai")


class BaiduConnector:
    """Collect source facts from Baidu's public SSR list and JSON endpoint."""

    source_key = "baidu_cn"

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
        recruit_type = {
            Channel.CAMPUS: "GRADUATE",
            Channel.INTERNSHIP: "INTERN",
        }.get(channel)
        if recruit_type is None:
            raise ValueError("Baidu connector supports campus and internship channels")

        jobs_by_id: dict[str, JobRecord] = {}
        target_total: int | None = None
        partition_counts: dict[str, int] = {}
        complete = False
        last_error: Exception | None = None

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    await asyncio.sleep(2)
                current, total, pass_counts, complete = await self._collect_pass(
                    channel,
                    recruit_type,
                    max_pages=max_pages,
                    attempt=attempt,
                )
                partition_counts.update(pass_counts)
                if target_total != total:
                    target_total = total
                    jobs_by_id = {}
                for external_id, record in current.items():
                    previous = jobs_by_id.get(external_id)
                    if previous is not None and previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Baidu job {external_id} changed during collection"
                        )
                    jobs_by_id[external_id] = record
                if not complete:
                    break
                if target_total == len(jobs_by_id):
                    break
                last_error = RuntimeError(
                    f"Baidu list did not converge: declared={target_total}, "
                    f"unique={len(jobs_by_id)}"
                )
            except Exception as exc:
                last_error = exc
                if max_pages is not None or attempt == MAX_COLLECTION_ATTEMPTS:
                    raise
                continue

        if complete and target_total != len(jobs_by_id):
            raise RuntimeError(str(last_error))
        if target_total is None:
            raise RuntimeError("Baidu list returned no pagination metadata")
        partition_counts["all"] = target_total
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
        recruit_type: str,
        *,
        max_pages: int | None,
        attempt: int,
    ) -> tuple[dict[str, JobRecord], int, dict[str, int], bool]:
        payload = await self._fetch_page(recruit_type, 1)
        first = self._position_page(payload, expected_page=1)
        total = first["total"]
        pages = first["pages"]
        records: dict[str, JobRecord] = {}
        partition_counts = {"root": total}
        complete = True

        for page_number in range(1, pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            if page_number > 1:
                payload = await self._fetch_page(recruit_type, page_number)
            current = self._position_page(payload, expected_page=page_number)
            if current["total"] != total:
                raise RuntimeError(
                    f"Baidu total changed during pass: {total} -> {current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(
                channel,
                "root" if attempt == 1 else f"root-retry-{attempt}",
                page_number - 1,
                payload,
            )
            for raw in current["rows"]:
                record = self.parse_job(raw, channel)
                previous = records.get(record.external_id)
                if previous is not None and previous.content_hash() != record.content_hash():
                    raise RuntimeError(
                        f"Baidu job {record.external_id} changed within a page pass"
                    )
                records[record.external_id] = record

        return records, total, partition_counts, complete

    async def _fetch_page(self, recruit_type: str, page_number: int) -> dict[str, Any]:
        delay = self.settings.baidu_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)
        request_body = {
            "recruitType": recruit_type,
            "pageSize": str(PAGE_SIZE),
            "keyWord": "",
            "curPage": str(page_number),
            "projectType": "",
        }
        # Baidu validates the browser session that rendered the public page.
        # Use page-context fetch so its anonymous session/cookies and Referer
        # are retained; a separate APIRequestContext is rejected as illegal visit.
        payload = None
        for recovery_attempt in range(3):
            response = await self.page.request.post(
                API_URL,
                form=request_body,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://talent.baidu.com",
                    "Referer": LIST_URL,
                    "User-Agent": COLLECTOR_USER_AGENT,
                },
                timeout=RESPONSE_TIMEOUT_SECONDS * 1000,
            )
            self._last_request_at = asyncio.get_running_loop().time()
            if response.status != 200:
                raise RuntimeError(f"Baidu endpoint returned HTTP {response.status}")
            payload = await response.json()
            if isinstance(payload, dict) and payload.get("status") != "no-auth":
                break
            if recovery_attempt < 2:
                await asyncio.sleep(2)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid Baidu response for page {page_number}")
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Baidu response for page {page_number}: {message}")
        return payload

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Baidu response has no data object")
        total = _required_int(data.get("total"), "total", allow_zero=True)
        page_number = _required_int(data.get("pageNum"), "pageNum")
        page_size = _required_int(data.get("pageSize"), "pageSize")
        pages = _required_int(data.get("pages"), "pages", allow_zero=True)
        rows = data.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Baidu response has an invalid list")
        if page_number != expected_page:
            raise RuntimeError(
                f"Baidu page mismatch: expected={expected_page}, got={page_number}"
            )
        expected_pages = ceil(total / page_size) if total else 0
        if pages != expected_pages:
            raise RuntimeError(
                f"Baidu pagination metadata is inconsistent: pages={pages}, "
                f"total={total}, page_size={page_size}"
            )
        expected_rows = min(page_size, max(total - (page_number - 1) * page_size, 0))
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Baidu page {page_number} row mismatch: expected={expected_rows}, "
                f"got={len(rows)}"
            )
        return {"rows": rows, "total": total, "pages": pages}

    @staticmethod
    def parse_job(raw: dict[str, Any], channel: Channel) -> JobRecord:
        external_id = _required(raw, "postId", "Baidu post id")
        title = _required(raw, "name", f"Baidu job title ({external_id})")
        project_name = _required(raw, "projectType", f"Baidu project ({external_id})")
        project_id = _required(raw, "projectTypeCode", f"Baidu project code ({external_id})")
        recruit_type = "GRADUATE" if channel is Channel.CAMPUS else "INTERN"
        # The GRADUATE list officially includes several named campus projects
        # (校招、AIDU项目、管培生项目). Keep the exact project label instead of
        # collapsing it or rejecting a valid campus row.
        locations = [
            LocationRecord(code=f"name:{name}", name=name)
            for name in _split_locations(raw.get("workPlace"))
        ]
        category_name = _optional(raw.get("postType"))
        return JobRecord(
            source_key="baidu_cn",
            external_id=external_id,
            external_code=_optional(raw.get("jobId")),
            source_url=LIST_URL,
            company_name="百度",
            channel=channel,
            employment_type_id=recruit_type,
            employment_type_name=project_name,
            recruitment_project_id=project_id,
            recruitment_project_name=project_name,
            title=title,
            description=_optional(raw.get("workContent")),
            requirements=_optional(raw.get("serviceCondition")),
            published_at=_date_only(raw.get("publishDate"), "publishDate"),
            source_updated_at=_date_only(raw.get("updateDate"), "updateDate"),
            recruitment_count=_recruitment_count(raw.get("recruitNum")),
            locations=locations,
            categories=(
                []
                if category_name is None
                else [
                    SourceCategoryRecord(
                        external_id=f"name:{category_name}",
                        name=category_name,
                        assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                    )
                ]
            ),
            is_hot=_optional_bool(raw.get("hotFlag")),
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


def _required_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Baidu {field} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Baidu {field} is not an integer: {value!r}") from exc
    if parsed < (0 if allow_zero else 1):
        raise RuntimeError(f"Baidu {field} is invalid: {value!r}")
    return parsed


def _date_only(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError(f"Baidu {field} is not YYYY-MM-DD: {text!r}") from exc


def _split_locations(value: Any) -> list[str]:
    text = _optional(value)
    if text is None:
        return []
    values = [part.strip() for part in re.split(r"[、,，;/；]+", text) if part.strip()]
    return list(dict.fromkeys(values))


def _recruitment_count(value: Any) -> int | None:
    text = _optional(value)
    if text is None or text in {"0", "若干"}:
        return None
    try:
        count = int(text)
    except ValueError as exc:
        raise ValueError(f"Baidu recruitment count is invalid: {value!r}") from exc
    if count < 0:
        raise ValueError(f"Baidu recruitment count is invalid: {value!r}")
    return count


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"Baidu hot flag is not boolean: {value!r}")
