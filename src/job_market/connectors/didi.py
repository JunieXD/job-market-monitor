"""Didi's anonymous social-recruitment JSON endpoints."""

import asyncio
import json
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode
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

BASE_URL = "https://talent.didiglobal.com"
LIST_URL = f"{BASE_URL}/social/list/1"
LIST_ENDPOINT = f"{BASE_URL}/recruit-portal-service/api/job/front/list"
DETAIL_ENDPOINT = f"{BASE_URL}/recruit-portal-service/api/job/front/view"
TYPE_ENDPOINT = f"{BASE_URL}/recruit-portal-service/api/job/jdpublish/confirm/listJdTypes"
PAGE_SIZE = 16
MAX_COLLECTION_ATTEMPTS = 5
MAX_REQUEST_ATTEMPTS = 3
MIN_LIVE_LIST_COVERAGE = 0.98
RESPONSE_TIMEOUT_SECONDS = 30
SHANGHAI = ZoneInfo("Asia/Shanghai")


class DidiConnector:
    """Collect Didi social jobs without login or candidate-side actions."""

    source_key = "didi_social_cn"

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
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Didi connector supports only experienced jobs")
        await self.page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
        categories = await self._fetch_categories()

        rows_by_id: dict[str, dict[str, Any]] = {}
        target_total: int | None = None
        partition_counts: dict[str, int] = {}
        complete = False
        absence_authoritative = False
        last_error: Exception | None = None
        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            try:
                if max_pages is not None:
                    current, total, counts, complete = await self._collect_root_pass(
                        channel,
                        max_pages=max_pages,
                        attempt=attempt,
                    )
                    consistent = False
                else:
                    current, total, counts, consistent = (
                        await self._collect_partitioned_pass(
                            channel,
                            categories,
                            attempt=attempt,
                        )
                    )
                    complete = True
                target_total = total
                rows_by_id = current
                partition_counts = counts
                if consistent:
                    absence_authoritative = True
                    break
                if not complete:
                    break
                last_error = RuntimeError(
                    f"Didi category partitions did not converge: declared={target_total}, "
                    f"unique={len(rows_by_id)}"
                )
            except Exception as exc:
                last_error = exc
                if max_pages is not None or attempt == MAX_COLLECTION_ATTEMPTS:
                    raise

        if target_total is None:
            raise RuntimeError("Didi list returned no pagination metadata")
        if complete and not absence_authoritative:
            coverage = len(rows_by_id) / target_total if target_total else 1.0
            if coverage < MIN_LIVE_LIST_COVERAGE:
                raise RuntimeError(str(last_error))
            # The social list is live and can change while category partitions
            # are in progress. Keep the traversed rows when coverage is high,
            # and expose the discrepancy to quality checks instead of
            # pretending the declared total was an immutable snapshot.
            partition_counts["list-consistency-warning"] = 1
            partition_counts["list-missing-estimate"] = max(
                target_total - len(rows_by_id),
                0,
            )
            partition_counts["list-over-declared"] = max(
                len(rows_by_id) - target_total,
                0,
            )

        jobs: list[JobRecord] = []
        detail_batch: list[dict[str, Any]] = []
        ordered_rows = list(rows_by_id.values())
        for index, row in enumerate(ordered_rows, start=1):
            detail = await self._fetch_detail(row["jdId"])
            record = self.parse_job(row, detail, categories, channel)
            jobs.append(record)
            detail_batch.append({"list": row, "detail": detail})
            if len(detail_batch) == PAGE_SIZE or index == len(ordered_rows):
                self._save_payload(
                    channel,
                    "details",
                    (index - 1) // PAGE_SIZE,
                    {"items": detail_batch},
                )
                detail_batch = []

        partition_counts["all"] = target_total
        partition_counts["collected-unique"] = len(jobs)
        return CollectionResult(
            channel=channel,
            jobs=jobs,
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
            absence_authoritative=absence_authoritative,
        )

    async def _collect_root_pass(
        self,
        channel: Channel,
        *,
        max_pages: int | None,
        attempt: int,
    ) -> tuple[dict[str, dict[str, Any]], int, dict[str, int], bool]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        first_payload = await self._fetch_list_page(1)
        first = self._position_page(first_payload, expected_page=1)
        total = first["total"]
        pages = ceil(total / PAGE_SIZE) if total else 0
        complete = True
        for page_number in range(1, pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            payload = (
                first_payload
                if page_number == 1
                else await self._fetch_list_page(page_number)
            )
            current = self._position_page(payload, expected_page=page_number)
            if current["total"] != total:
                raise RuntimeError(
                    f"Didi total changed during pass: {total} -> {current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(
                channel,
                "root" if attempt == 1 else f"root-retry-{attempt}",
                page_number - 1,
                payload,
            )
            for row in current["rows"]:
                external_id = _required_id(row.get("jdId"), "Didi job id")
                previous = rows_by_id.get(external_id)
                if previous is not None and _stable_row(previous) != _stable_row(row):
                    raise RuntimeError(f"Didi job {external_id} changed within a pass")
                rows_by_id[external_id] = row
        return rows_by_id, total, {"root": total}, complete

    async def _collect_partitioned_pass(
        self,
        channel: Channel,
        categories: dict[str, str],
        *,
        attempt: int,
    ) -> tuple[dict[str, dict[str, Any]], int, dict[str, int], bool]:
        before_payload = await self._fetch_list_page(1)
        before = self._position_page(before_payload, expected_page=1)
        root_before = before["total"]
        self.pages_fetched += 1
        self._save_payload(
            channel,
            "root-summary-before" if attempt == 1 else f"root-summary-before-{attempt}",
            0,
            before_payload,
        )

        rows_by_id: dict[str, dict[str, Any]] = {}
        partition_counts: dict[str, int] = {"root-before": root_before}
        categories_consistent = True
        for category_code in categories:
            category_rows, total, category_consistent = (
                await self._collect_category_partition(
                    channel,
                    category_code,
                    collection_attempt=attempt,
                )
            )
            categories_consistent = categories_consistent and category_consistent
            partition = f"category-{category_code}"
            partition_counts[partition] = total
            for external_id, row in category_rows.items():
                previous = rows_by_id.get(external_id)
                if previous is not None:
                    if _stable_row(previous) != _stable_row(row):
                        raise RuntimeError(
                            f"Didi job {external_id} changed category during collection"
                        )
                    raise RuntimeError(
                        f"Didi job {external_id} appeared in multiple categories"
                    )
                rows_by_id[external_id] = row

        after_payload = await self._fetch_list_page(1)
        after = self._position_page(after_payload, expected_page=1)
        root_after = after["total"]
        partition_counts["root-after"] = root_after
        self.pages_fetched += 1
        self._save_payload(
            channel,
            "root-summary-after" if attempt == 1 else f"root-summary-after-{attempt}",
            0,
            after_payload,
        )
        category_total = sum(
            count
            for partition, count in partition_counts.items()
            if partition.startswith("category-")
        )
        consistent = (
            categories_consistent
            and root_before == root_after == category_total == len(rows_by_id)
        )
        partition_counts["category-sum"] = category_total
        return rows_by_id, root_after, partition_counts, consistent

    async def _collect_category_partition(
        self,
        channel: Channel,
        category_code: str,
        *,
        collection_attempt: int,
    ) -> tuple[dict[str, dict[str, Any]], int, bool]:
        best_rows: dict[str, dict[str, Any]] = {}
        best_total: int | None = None
        last_error: Exception | None = None
        for partition_attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            rows_by_id: dict[str, dict[str, Any]] = {}
            duplicate_seen = False
            try:
                first_payload = await self._fetch_list_page(
                    1,
                    job_type=category_code,
                )
                first = self._position_page(first_payload, expected_page=1)
                total = first["total"]
                pages = ceil(total / PAGE_SIZE) if total else 0
                if pages == 0:
                    pages = 1
                partition = (
                    f"category-{category_code}-pass-{partition_attempt}"
                    if collection_attempt == 1
                    else (
                        f"category-{category_code}-collection-{collection_attempt}"
                        f"-pass-{partition_attempt}"
                    )
                )
                for page_number in range(1, pages + 1):
                    payload = (
                        first_payload
                        if page_number == 1
                        else await self._fetch_list_page(
                            page_number,
                            job_type=category_code,
                        )
                    )
                    current = self._position_page(payload, expected_page=page_number)
                    if current["total"] != total:
                        raise RuntimeError(
                            f"Didi category-{category_code} total changed: "
                            f"{total} -> {current['total']}"
                        )
                    self.pages_fetched += 1
                    self._save_payload(
                        channel,
                        partition,
                        page_number - 1,
                        payload,
                    )
                    for row in current["rows"]:
                        external_id = _required_id(row.get("jdId"), "Didi job id")
                        actual_category = _required_id(
                            row.get("jobType"),
                            f"Didi job category ({external_id})",
                        )
                        if actual_category != category_code:
                            raise RuntimeError(
                                f"Didi {external_id} appeared in category "
                                f"{category_code} with jobType={actual_category}"
                            )
                        previous = rows_by_id.get(external_id)
                        if previous is not None:
                            if _stable_row(previous) != _stable_row(row):
                                raise RuntimeError(
                                    f"Didi job {external_id} changed within "
                                    f"category {category_code}"
                                )
                            duplicate_seen = True
                        rows_by_id[external_id] = row

                if best_total is None or len(rows_by_id) > len(best_rows):
                    best_rows = rows_by_id
                    best_total = total
                if not duplicate_seen and len(rows_by_id) == total:
                    return rows_by_id, total, True
                last_error = RuntimeError(
                    f"Didi category-{category_code} did not converge: "
                    f"declared={total}, unique={len(rows_by_id)}, "
                    f"duplicate_seen={duplicate_seen}"
                )
            except Exception as exc:
                last_error = exc

            if partition_attempt < MAX_COLLECTION_ATTEMPTS:
                await asyncio.sleep(partition_attempt)

        if best_total is None:
            raise RuntimeError(
                f"Didi category-{category_code} could not be collected"
            ) from last_error
        return best_rows, best_total, False

    async def _fetch_categories(self) -> dict[str, str]:
        payload = await self._request_json(TYPE_ENDPOINT, method="GET")
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Didi job type catalog is empty")
        categories: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Didi job type catalog contains a non-object")
            code = _required_id(row.get("code"), "Didi category code")
            name = _required(row, "name", "Didi category name")
            if code in categories and categories[code] != name:
                raise RuntimeError(f"Didi category {code} changed name")
            categories[code] = name
        return categories

    async def _fetch_list_page(
        self,
        page_number: int,
        *,
        job_type: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "page": page_number,
            "recruitType": 1,
            "size": PAGE_SIZE,
        }
        if job_type is not None:
            params["jobType"] = job_type
        return await self._request_json(
            f"{LIST_ENDPOINT}?{urlencode(params)}",
            method="GET",
        )

    async def _fetch_detail(self, job_id: str) -> dict[str, Any]:
        payload = await self._request_json(f"{DETAIL_ENDPOINT}/{job_id}", method="GET")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Didi detail {job_id} has no data object")
        return data

    async def _request_json(self, url: str, *, method: str) -> dict[str, Any]:
        response: object = None
        last_error: Exception | None = None
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            delay = self.settings.didi_request_delay_seconds - (
                asyncio.get_running_loop().time() - self._last_request_at
            )
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await self.page.evaluate(
                    """async ({url, method, timeoutMs}) => {
                        const controller = new AbortController();
                        const timer = setTimeout(() => controller.abort(), timeoutMs);
                        try {
                            const response = await fetch(url, {
                                method,
                                credentials: 'include',
                                headers: {'Accept': 'application/json, text/plain, */*'},
                                signal: controller.signal,
                            });
                            return {status: response.status, payload: await response.json()};
                        } finally {
                            clearTimeout(timer);
                        }
                    }""",
                    {
                        "url": url,
                        "method": method,
                        "timeoutMs": RESPONSE_TIMEOUT_SECONDS * 1000,
                    },
                )
            except Exception as exc:
                last_error = exc
                response = None
            finally:
                self._last_request_at = asyncio.get_running_loop().time()

            status = response.get("status") if isinstance(response, dict) else None
            retryable_status = status == 429 or (
                isinstance(status, int) and 500 <= status < 600
            )
            if last_error is None and not retryable_status:
                break
            if attempt == MAX_REQUEST_ATTEMPTS:
                if last_error is not None:
                    raise RuntimeError(
                        f"Didi endpoint request failed after {attempt} attempts: {url}"
                    ) from last_error
                break
            await asyncio.sleep(attempt)
            last_error = None

        if not isinstance(response, dict) or response.get("status") != 200:
            status = response.get("status") if isinstance(response, dict) else response
            raise RuntimeError(f"Didi endpoint returned HTTP {status}: {url}")
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Didi endpoint returned non-object JSON: {url}")
        meta = payload.get("meta")
        if not isinstance(meta, dict) or meta.get("code") != 0:
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Didi response: {message}")
        return payload

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Didi list response has no data object")
        total = _required_int(data.get("total"), "total", allow_zero=True)
        page = _required_int(data.get("page"), "page")
        size = _required_int(data.get("size"), "size")
        rows = data.get("items")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Didi list response has an invalid items list")
        if total == 0 and rows:
            raise RuntimeError("Didi list returned rows with a zero total; retrying")
        if page != expected_page or size != PAGE_SIZE:
            raise RuntimeError(
                "Didi list pagination mismatch: "
                f"expected page/size={expected_page}/{PAGE_SIZE}, "
                f"got={page}/{size}"
            )
        pages = ceil(total / PAGE_SIZE) if total else 0
        expected_rows = min(PAGE_SIZE, max(total - (page - 1) * PAGE_SIZE, 0))
        # Didi's list is live while we paginate. The final page can lose or
        # gain a few rows as jobs refresh; require full pages before the final
        # page, but let the convergence retry determine whether the final
        # page's shorter snapshot still yields the declared union.
        if page < pages and len(rows) != PAGE_SIZE:
            raise RuntimeError(
                f"Didi page {page} row mismatch: expected={expected_rows}, got={len(rows)}"
            )
        if page == pages and len(rows) > PAGE_SIZE:
            raise RuntimeError(
                f"Didi final page {page} row overflow: got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(
        row: dict[str, Any],
        detail: dict[str, Any],
        categories: dict[str, str],
        channel: Channel,
    ) -> JobRecord:
        external_id = _required_id(row.get("jdId"), "Didi job id")
        title = _required(detail, "jobName", f"Didi job title ({external_id})")
        category_code = _required_id(row.get("jobType"), f"Didi category ({external_id})")
        category_name = _required(detail, "jobType", f"Didi category name ({external_id})")
        catalog_name = categories.get(category_code)
        if catalog_name is not None and catalog_name != category_name:
            raise ValueError(
                f"Didi category {category_code} changed name: {catalog_name!r} -> {category_name!r}"
            )
        department = _optional(detail.get("deptName"))
        recruitment_type = _required(
            detail,
            "recruitType",
            f"Didi recruitment type ({external_id})",
        )
        return JobRecord(
            source_key="didi_social_cn",
            external_id=external_id,
            external_code=_optional(detail.get("jdNo")),
            source_url=f"{BASE_URL}/social/p/{row.get('jdId')}",
            company_name="滴滴",
            channel=channel,
            employment_type_id=recruitment_type,
            employment_type_name="社会招聘",
            title=title,
            description=_optional(detail.get("jobDesc")),
            requirements=_optional(detail.get("qualification")),
            published_at=_datetime(detail.get("publishTime"), "publishTime"),
            source_updated_at=_datetime(detail.get("refreshTime"), "refreshTime"),
            source_status=_optional(detail.get("jdStatus")),
            recruitment_count=_optional_int(detail.get("recruitNum"), "recruitNum"),
            department_name=department,
            locations=[
                LocationRecord(code=f"name:{name}", name=name)
                for name in _split_locations(detail.get("workArea"))
            ],
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload={"list": row, "detail": detail},
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


def _stable_row(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("jdId"),
        row.get("jdNo"),
        row.get("jobName"),
        row.get("workArea"),
        row.get("deptName"),
        row.get("jobType"),
        row.get("refreshTime"),
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


def _required_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} is invalid: {value!r}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} is invalid: {value!r}")
    return text


def _required_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"Didi {field} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Didi {field} is not an integer: {value!r}") from exc
    if parsed < (0 if allow_zero else 1):
        raise RuntimeError(f"Didi {field} is invalid: {value!r}")
    return parsed


def _datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Didi {field} is not an ISO datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _split_locations(value: Any) -> list[str]:
    text = _optional(value)
    if text is None:
        return []
    return list(
        dict.fromkeys(
            part.strip() for part in text.replace("/", " ").split() if part.strip()
        )
    )


def _optional_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Didi {field} is invalid: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Didi {field} is invalid: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"Didi {field} is invalid: {value!r}")
    return parsed
