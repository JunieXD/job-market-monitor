"""Cainiao Group public experienced-recruitment connector."""

import asyncio
import json
from math import ceil
from typing import Any

from playwright.async_api import Page, Response

from job_market.config import Settings
from job_market.connectors.alibaba_common import (
    canonical_position_url,
    coded_label,
    number_range,
    time_range,
    timestamp_ms,
    unique_strings,
)
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
UI_PAGE_SIZE = 10
MAX_COLLECTION_ATTEMPTS = 5
POSITION_PAGE_URL = "https://talent.cainiao.com/off-campus/"
POSITION_URL = "https://talent.cainiao.com/off-campus-position/{external_id}"
POSITION_ENDPOINT = "/position/search"


class CainiaoConnector:
    """Collect Cainiao's public social list through rendered pagination."""

    source_key = "alibaba_cainiao_social"
    company_name = "菜鸟集团"
    portal_name = "Cainiao"
    position_page_url = POSITION_PAGE_URL
    position_url = POSITION_URL
    request_delay_setting = "cainiao_request_delay_seconds"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError(f"{self.portal_name} connector supports only experienced jobs")

        initial_payload = await self._open_root()
        initial_total = self._position_page(
            initial_payload,
            expected_page=1,
        )["total"]
        jobs_by_id, declared_total, complete = await self._collect_root(
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
                "all": declared_total,
                "root-initial": initial_total,
                "collected-unique": len(jobs_by_id),
            },
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _collect_root(
        self,
        channel: Channel,
        initial_payload: dict[str, Any],
        initial_total: int,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool]:
        union_by_id: dict[str, JobRecord] = {}
        target_total = initial_total

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = initial_payload if attempt == 1 else await self._open_root()
            first = self._position_page(payload, expected_page=1)
            if first["total"] != target_total:
                target_total = first["total"]
                union_by_id = {}
            total_pages = ceil(target_total / UI_PAGE_SIZE) if target_total else 0
            pass_by_id: dict[str, JobRecord] = {}
            attempt_label = "root" if attempt == 1 else f"root-retry-{attempt}"
            if total_pages == 0:
                self._save_payload(channel, attempt_label, 0, payload)

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, target_total, False
                await self._assert_active_page(page_number)
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != target_total:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    attempt_label,
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = pass_by_id.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"{self.portal_name} job {record.external_id} changed within "
                            f"{attempt_label}"
                        )
                    pass_by_id[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._next_page(page_number + 1)
            else:
                for external_id, record in pass_by_id.items():
                    previous = union_by_id.get(external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"{self.portal_name} job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        f"{self.portal_name} observations exceeded the declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            f"{self.portal_name} list did not converge after {MAX_COLLECTION_ATTEMPTS} "
            f"attempts: declared={target_total}, union={len(union_by_id)}"
        )

    async def _open_root(self) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = 1
        await self.page.goto(
            self.position_page_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_payload("positions:1")
        await self._assert_active_page(1)
        return payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_item = self.page.locator(".kuma-page-next")
        if await next_item.count() != 1:
            raise RuntimeError(
                f"{self.portal_name} pagination has no unique next control"
            )
        classes = await next_item.get_attribute("class") or ""
        if "kuma-page-disabled" in classes:
            raise RuntimeError(
                f"{self.portal_name} pagination ended before page {page_number}"
            )
        await next_item.click()
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".kuma-page-item-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"{self.portal_name} page did not render active page {expected_page}"
        )

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"{self.portal_name} response: {operation}",
        )
        if payload.get("success") is not True or not isinstance(
            payload.get("content"), dict
        ):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(
                f"Invalid {self.portal_name} response for {operation}: {message}"
            )
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @classmethod
    def _position_page(
        cls,
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise RuntimeError(f"{cls.portal_name} position response has no content")
        total = _response_int(content.get("totalCount"), "totalCount", allow_zero=True)
        page_number = _response_int(content.get("currentPage"), "currentPage")
        page_size = _response_int(content.get("pageSize"), "pageSize")
        rows = content.get("datas")
        if page_number != expected_page:
            raise RuntimeError(
                f"{cls.portal_name} page mismatch: expected={expected_page}, "
                f"got={page_number}"
            )
        if page_size != UI_PAGE_SIZE:
            raise RuntimeError(
                f"{cls.portal_name} page size changed: expected={UI_PAGE_SIZE}, "
                f"got={page_size}"
            )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError(
                f"{cls.portal_name} position response has invalid datas"
            )
        expected_rows = min(
            page_size,
            max(total - (expected_page - 1) * page_size, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"{cls.portal_name} page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @classmethod
    def parse_job(cls, raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "id", f"{cls.portal_name} job id")
        title = _required(
            raw,
            "name",
            f"{cls.portal_name} job title ({external_id})",
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
        graduation_start_at, graduation_end_at = time_range(
            raw.get("graduationTime"),
            "graduationTime",
        )
        category_names = unique_strings(raw.get("categories"), "categories")

        return JobRecord(
            source_key=cls.source_key,
            external_id=external_id,
            external_code=_optional(raw.get("code")),
            source_url=canonical_position_url(
                raw.get("positionUrl"),
                cls.position_url.format(external_id=external_id),
                cls.position_page_url,
            ),
            company_name=cls.company_name,
            channel=Channel.EXPERIENCED,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=title,
            description=_optional(raw.get("description")),
            requirements=_optional(raw.get("requirement")),
            published_at=timestamp_ms(raw.get("publishTime"), "publishTime"),
            source_updated_at=timestamp_ms(raw.get("modifyTime"), "modifyTime"),
            source_status=_optional(raw.get("status")),
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
            locations=[
                LocationRecord(code=f"city:{name}", name=name)
                for name in unique_strings(raw.get("workLocations"), "workLocations")
            ],
            categories=[
                SourceCategoryRecord(
                    external_id=f"label:{name}",
                    name=name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
                for name in category_names
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200 or POSITION_ENDPOINT not in response.url:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        if not isinstance(post_data, dict):
            return
        csrf = _optional(post_data.get("_csrf"))
        expected = {
            "key": "",
            "pageSize": UI_PAGE_SIZE,
            "pageIndex": self._active_page,
            "channel": "group_official_site",
            "language": "zh",
            "_csrf": csrf,
        }
        if csrf is not None and post_data == expected:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        request_delay = getattr(self.settings, self.request_delay_setting)
        delay = request_delay - (
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


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Alibaba portal {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Alibaba portal {field} is invalid: {value!r}")
    return value
