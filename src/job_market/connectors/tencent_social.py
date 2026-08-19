"""Tencent public experienced-recruitment connector."""

import asyncio
import json
import re
import time
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

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

RESPONSE_TIMEOUT_SECONDS = 30
UI_PAGE_SIZE = 100
MAX_COLLECTION_ATTEMPTS = 5
POSITION_DETAIL_URL = "https://careers.tencent.com/jobdesc.html?postId={external_id}"
POSITION_API_URL = "https://careers.tencent.com/tencentcareer/api/post/Query"


class TencentSocialConnector:
    """Collect the official social root list through its public JSON endpoint."""

    source_key = "tencent_social_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._root_observations: list[int] = []

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Tencent social connector supports only experienced jobs")

        root_payload = await self._observe_social_root(channel)
        initial_total = self._position_page(root_payload, expected_page=1)["total"]
        jobs_by_id, declared_total, complete = await self._collect_root(
            channel,
            root_payload,
            initial_total,
            max_pages,
        )
        partition_counts = {
            "all": declared_total,
            "root-initial": initial_total,
            "collected-unique": len(jobs_by_id),
        }
        partition_counts.update(
            {
                f"root-observation-{index:02d}": total
                for index, total in enumerate(self._root_observations, start=1)
            }
        )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
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
            payload = (
                initial_payload
                if attempt == 1
                else await self._observe_social_root(channel)
            )

            first = self._position_page(payload, expected_page=1)
            if first["total"] != target_total:
                target_total = first["total"]
                union_by_id = {}

            pass_by_id: dict[str, JobRecord] = {}
            total_pages = ceil(target_total / UI_PAGE_SIZE) if target_total else 0
            attempt_label = "root" if attempt == 1 else f"root-retry-{attempt}"
            if total_pages == 0:
                self._save_payload(channel, attempt_label, 0, payload)

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union_by_id or pass_by_id, target_total, False
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
                            f"Tencent social job {record.external_id} changed within "
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
                            f"Tencent social job {external_id} changed during retries"
                        )
                    union_by_id[external_id] = record
                if len(union_by_id) == target_total:
                    return union_by_id, target_total, True
                if len(union_by_id) > target_total:
                    raise RuntimeError(
                        "Tencent social observations exceeded the declared total: "
                        f"declared={target_total}, unique={len(union_by_id)}"
                    )

        raise RuntimeError(
            f"Tencent social root did not converge after "
            f"{MAX_COLLECTION_ATTEMPTS} attempts: declared={target_total}, "
            f"union={len(union_by_id)}"
        )

    async def _observe_social_root(self, channel: Channel) -> dict[str, Any]:
        payload = await self._open_social_root()
        root = self._position_page(payload, expected_page=1)
        self._root_observations.append(root["total"])
        return payload

    async def _open_social_root(self) -> dict[str, Any]:
        return await self._fetch_page(1)

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        return await self._fetch_page(page_number)

    async def _fetch_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        query = urlencode(
            {
                "timestamp": str(round(time.time() * 1000)),
                "countryId": "",
                "cityId": "",
                "bgIds": "",
                "productId": "",
                "categoryId": "",
                "parentCategoryId": "",
                "attrId": "1",
                "keyword": "",
                "pageIndex": str(page_number),
                "pageSize": str(UI_PAGE_SIZE),
                "language": "zh-cn",
                "area": "cn",
            }
        )
        response = await self.page.goto(
            f"{POSITION_API_URL}?{query}",
            wait_until="commit",
            timeout=60_000,
        )
        if response is None or response.status != 200:
            status = None if response is None else response.status
            raise RuntimeError(
                f"Tencent social page {page_number} returned HTTP {status}"
            )
        payload = await response.json()
        if payload.get("Code") != 200 or not isinstance(payload.get("Data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(
                f"Invalid Tencent social response for page {page_number}: {message}"
            )
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        data = payload.get("Data")
        if not isinstance(data, dict):
            raise RuntimeError("Tencent social response has no data object")
        rows = data.get("Posts")
        total = _response_int(data.get("Count"), "Count", allow_zero=True)
        if not isinstance(rows, list):
            raise RuntimeError("Tencent social response has no post list")
        expected_rows = min(
            UI_PAGE_SIZE,
            max(total - (expected_page - 1) * UI_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Tencent social page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Tencent social page {expected_page} contains a non-object row"
                )
        return {"rows": rows, "total": total}

    @staticmethod
    def parse_job(raw: dict[str, Any]) -> JobRecord:
        external_id = _required(raw, "PostId", "Tencent social job id")
        title = _required(
            raw,
            "RecruitPostName",
            f"Tencent social job title ({external_id})",
        )
        country = _required(
            raw,
            "CountryName",
            f"Tencent social country ({external_id})",
        )
        location = _required(
            raw,
            "LocationName",
            f"Tencent social location ({external_id})",
        )
        category = _optional(raw.get("CategoryName"))
        business_name = _optional(raw.get("BGName"))
        experience_min = _minimum_experience_years(raw.get("RequireWorkYearsName"))

        return JobRecord(
            source_key="tencent_social_cn",
            external_id=external_id,
            external_code=_optional(raw.get("RecruitPostId")),
            source_url=POSITION_DETAIL_URL.format(external_id=external_id),
            company_name="腾讯",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=title,
            description=_optional(raw.get("Responsibility")),
            requirements=None,
            source_updated_at=_source_datetime(raw.get("LastUpdateTime")),
            experience_min_years=experience_min,
            locations=[
                LocationRecord(
                    code=f"name:{country}/{location}",
                    name=location,
                    country_name=country,
                )
            ],
            categories=(
                []
                if category is None
                else [
                    SourceCategoryRecord(
                        external_id=f"label:{category}",
                        name=category,
                        assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                    )
                ]
            ),
            business_units=(
                []
                if business_name is None
                else [
                    BusinessUnitRecord(
                        code=f"name:{business_name}",
                        name=business_name,
                    )
                ]
            ),
            source_payload=raw,
        )

    async def _rate_limit(self) -> None:
        delay = self.settings.tencent_request_delay_seconds - (
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
        raise RuntimeError(f"Tencent social {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Tencent social {field} is invalid: {value!r}")
    return value


def _source_datetime(value: Any) -> datetime | None:
    text_value = _optional(value)
    if text_value is None:
        return None
    try:
        parsed = datetime.strptime(text_value, "%Y年%m月%d日")
    except ValueError as exc:
        raise ValueError(
            f"Tencent social LastUpdateTime is invalid: {text_value!r}"
        ) from exc
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def _minimum_experience_years(value: Any) -> int | None:
    text_value = _optional(value)
    if text_value is None or text_value == "不限":
        return None
    match = re.fullmatch(
        r"([一二两三四五六七八九十])年以上工作经验",
        text_value,
    )
    if match is None:
        return None
    return {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }[match.group(1)]
