"""Tongcheng Travel public experienced-recruitment connector."""

import asyncio
import json
from datetime import date, datetime, time
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
PAGE_SIZE = 10
MAX_COLLECTION_ATTEMPTS = 5
COMPANY_ID = "0583d"
POSITION_LIST_URL = "https://mhr.ly.com/recruit/portal/#/socialJob"
POSITION_DETAIL_URL = (
    "https://mhr.ly.com/recruit/portal/#/socialDetail?id={external_id}&type=4"
)
POSITION_ENDPOINT = "/recruit/ats/portal/societyJobList"
CATEGORY_ENDPOINT = "/recruit/ats/portal/category"


class TongchengConnector:
    """Collect Tongcheng's social list and official job-type filters."""

    source_key = "tongcheng_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self._active_category_id: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Tongcheng connector supports only experienced jobs")

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
                category_name,
                jobs_by_id,
                max_pages,
            )
            partition_counts[f"category:{category_id}"] = total
            assignment = SourceCategoryRecord(
                external_id=f"job-type:{category_id}",
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
            payload = await self._open_partition(None, None)
            first = self._position_page(payload, expected_page=1)
            if target_total != first["total"]:
                target_total = first["total"]
                union = {}
            pages = first["pages"]
            current_pass: dict[str, JobRecord] = {}
            partition = "root" if attempt == 1 else f"root-retry-{attempt}"

            for page_number in range(1, pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union or current_pass, target_total, False
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != target_total or current["pages"] != pages:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    Channel.EXPERIENCED,
                    partition,
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = current_pass.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Tongcheng job {record.external_id} changed within "
                            f"{partition}"
                        )
                    current_pass[record.external_id] = record
                if page_number < pages:
                    payload = await self._next_page(page_number + 1)
            else:
                self._merge_records(union, current_pass)
                if len(union) == target_total:
                    return union, target_total, True
                if len(union) > target_total:
                    raise RuntimeError(
                        "Tongcheng observations exceeded the declared total: "
                        f"{len(union)} > {target_total}"
                    )

        raise RuntimeError(
            "Tongcheng root list did not converge: "
            f"declared={target_total}, unique={len(union)}"
        )

    async def _collect_category_ids(
        self,
        category_id: str,
        category_name: str,
        jobs_by_id: dict[str, JobRecord],
        max_pages: int | None,
    ) -> tuple[set[str], int, bool]:
        union: set[str] = set()
        target_total: int | None = None

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = await self._open_partition(category_id, category_name)
            first = self._position_page(payload, expected_page=1)
            if target_total != first["total"]:
                target_total = first["total"]
                union = set()
            pages = first["pages"]
            current_pass: set[str] = set()
            partition = f"category-{category_id}-retry-{attempt}"

            for page_number in range(1, pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union or current_pass, target_total, False
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != target_total or current["pages"] != pages:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    Channel.EXPERIENCED,
                    partition,
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    root = jobs_by_id.get(record.external_id)
                    if root is None:
                        raise RuntimeError(
                            "Tongcheng category returned a job absent from the root: "
                            f"{record.external_id}"
                        )
                    if root.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Tongcheng job {record.external_id} changed between root "
                            f"and category {category_name!r}"
                        )
                    current_pass.add(record.external_id)
                if page_number < pages:
                    payload = await self._next_page(page_number + 1)
            else:
                union.update(current_pass)
                if len(union) == target_total:
                    return union, target_total, True
                if len(union) > target_total:
                    raise RuntimeError(
                        f"Tongcheng category {category_name!r} exceeded its total"
                    )

        raise RuntimeError(
            f"Tongcheng category {category_name!r} did not converge: "
            f"declared={target_total}, unique={len(union)}"
        )

    async def _open_partition(
        self,
        category_id: str | None,
        category_name: str | None,
    ) -> dict[str, Any]:
        self._active_page = 1
        self._active_category_id = None
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        drain_json_responses(self._category_responses)
        if self.page.url == POSITION_LIST_URL:
            await self.page.reload(
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        else:
            await self.page.goto(
                POSITION_LIST_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        root = await self._next_payload("root:1")
        if category_id is None:
            return root
        if category_name is None:
            raise ValueError("Tongcheng category name is required with its id")

        select = self.page.locator(".left .ant-select-selection--multiple")
        if await select.count() != 1:
            raise RuntimeError("Tongcheng category selector is not unique")
        await select.click()
        option = self.page.locator(
            ".ant-select-dropdown-menu-item",
            has_text=category_name,
        )
        exact = [
            option.nth(index)
            for index in range(await option.count())
            if (await option.nth(index).inner_text()).strip() == category_name
        ]
        if len(exact) != 1:
            raise RuntimeError(
                f"Tongcheng category {category_name!r} is not unique"
            )
        await exact[0].click()
        self._active_category_id = category_id
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        filter_button = self.page.locator(".left .btn", has_text="筛选职位")
        if await filter_button.count() != 1:
            raise RuntimeError("Tongcheng filter button is not unique")
        await filter_button.evaluate("element => element.click()")
        return await self._next_payload(f"category:{category_id}:1")

    async def _category_catalog(self) -> list[tuple[str, str]]:
        await self._open_partition(None, None)
        payload = await next_json_payload(
            self._category_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation="Tongcheng response: category catalog",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), list):
            raise RuntimeError("Tongcheng category response is invalid")
        catalog: list[tuple[str, str]] = []
        for raw in payload["data"]:
            if not isinstance(raw, dict):
                raise RuntimeError("Tongcheng category catalog has a non-object row")
            category_id = _required(raw, "id", "Tongcheng category id")
            category_name = _required(
                raw,
                "dictDetailName",
                f"Tongcheng category name ({category_id})",
            )
            catalog.append((category_id, category_name))
        if not catalog or len({item[0] for item in catalog}) != len(catalog):
            raise RuntimeError("Tongcheng category catalog is empty or duplicated")
        return catalog

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        self._active_page = page_number
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.locator("li.ant-pagination-next")
        if await next_button.count() != 1:
            raise RuntimeError("Tongcheng pagination has no unique next button")
        if await next_button.get_attribute("aria-disabled") == "true":
            raise RuntimeError(
                f"Tongcheng pagination ended before page {page_number}"
            )
        await next_button.evaluate("element => element.click()")
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator("li.ant-pagination-item-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"Tongcheng page did not render active page {expected_page}"
        )

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Tongcheng response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Tongcheng response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Tongcheng response has no data object")
        total = _response_int(data.get("totalElements"), "totalElements", allow_zero=True)
        pages = _response_int(data.get("totalPages"), "totalPages", allow_zero=True)
        rows = data.get("content")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Tongcheng response has invalid content")
        expected_pages = ceil(total / PAGE_SIZE) if total else 0
        if pages != expected_pages:
            raise RuntimeError(
                f"Tongcheng page-count mismatch: expected={expected_pages}, got={pages}"
            )
        expected_rows = min(
            PAGE_SIZE,
            max(total - (expected_page - 1) * PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Tongcheng page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total, "pages": pages}

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "id", "Tongcheng job id")
        title = _required(
            raw,
            "jobName",
            f"Tongcheng job title ({external_id})",
        )
        employment_type = _required(
            raw,
            "employmentType",
            f"Tongcheng employment type ({external_id})",
        )
        recruitment_type = _required(
            raw,
            "recruitmentTypeName",
            f"Tongcheng recruitment type ({external_id})",
        )
        if recruitment_type != "社会招聘":
            raise ValueError(
                f"Tongcheng social list returned {recruitment_type!r}"
            )
        return JobRecord(
            source_key="tongcheng_social_cn",
            external_id=external_id,
            source_url=POSITION_DETAIL_URL.format(external_id=external_id),
            company_name="同程旅行",
            channel=Channel.EXPERIENCED,
            employment_type_id=employment_type,
            employment_type_name=employment_type,
            title=title,
            description=None,
            requirements=None,
            published_at=_source_date(raw.get("createTime"), "createTime"),
            locations=[
                LocationRecord(code=f"name:{name}", name=name)
                for name in _work_locations(raw.get("workPlace"), external_id)
            ],
            categories=[],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        try:
            post_data = response.request.post_data_json
        except Exception:
            return
        if CATEGORY_ENDPOINT in response.url:
            if post_data == {"category": "jobtype"}:
                enqueue_json_response(self._category_responses, response)
            return
        if POSITION_ENDPOINT not in response.url:
            return
        expected = {
            "data": {
                "jobName": "",
                "companyId": COMPANY_ID,
                "workAddress": [""],
                "jobTypes": (
                    []
                    if self._active_category_id is None
                    else [self._active_category_id]
                ),
                "tableList": [],
                "recruitmentType": 2,
                "unitId": None,
            },
            "pageNo": self._active_page,
            "pageSize": PAGE_SIZE,
        }
        if not isinstance(post_data, dict):
            return
        normalized = dict(post_data)
        total = normalized.pop("total", None)
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            return
        if normalized == expected:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.tongcheng_request_delay_seconds - (
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
                    f"Tongcheng job {external_id} changed during pagination retries"
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


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Tongcheng {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Tongcheng {field} is invalid: {value!r}")
    return value


def _work_locations(value: Any, external_id: str) -> list[str]:
    text = _optional(value)
    if text is None:
        raise ValueError(f"Tongcheng job {external_id} has no workPlace")
    locations = list(dict.fromkeys(item.strip() for item in text.split(",") if item.strip()))
    if not locations:
        raise ValueError(f"Tongcheng job {external_id} has no work locations")
    return locations


def _source_date(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Tongcheng {field} is not an ISO date: {text!r}"
        ) from exc
    return datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))
