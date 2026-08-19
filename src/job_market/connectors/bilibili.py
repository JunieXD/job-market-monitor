"""Bilibili public experienced, campus, and internship connector."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class PortalConfig:
    channel: Channel
    path: str
    endpoint_group: str
    work_type_code: str
    employment_type_name: str

    @property
    def page_url(self) -> str:
        base = f"https://jobs.bilibili.com/{self.path}/positions"
        return f"{base}?type=0" if self.channel is Channel.INTERNSHIP else base


PORTALS = {
    Channel.EXPERIENCED: PortalConfig(
        channel=Channel.EXPERIENCED,
        path="social",
        endpoint_group="srs",
        work_type_code="3",
        employment_type_name="全职",
    ),
    Channel.CAMPUS: PortalConfig(
        channel=Channel.CAMPUS,
        path="campus",
        endpoint_group="campus",
        work_type_code="3",
        employment_type_name="全职",
    ),
    Channel.INTERNSHIP: PortalConfig(
        channel=Channel.INTERNSHIP,
        path="campus",
        endpoint_group="campus",
        work_type_code="0",
        employment_type_name="实习",
    ),
}


class BilibiliConnector:
    """Collect complete job-list responses through Bilibili's public UI."""

    source_key = "bilibili_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._category_responses: JsonResponseQueue = asyncio.Queue()
        self._active_endpoint_group: str | None = None
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        portal = PORTALS.get(channel)
        if portal is None:
            raise ValueError("Bilibili connector does not support this channel")

        payload, category_payload = await self._open_first_page(portal)
        categories = self.parse_category_tree(category_payload)
        first = self._position_page(payload, expected_page=1)
        jobs_by_id: dict[str, JobRecord] = {}
        complete = True

        if first["pages"] == 0:
            self._save_payload(channel, 1, payload)

        for page_number in range(1, first["pages"] + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                complete = False
                break
            await self._assert_active_page(page_number)
            current = self._position_page(payload, expected_page=page_number)
            self.pages_fetched += 1
            self._save_payload(channel, page_number, payload)
            for raw in current["rows"]:
                record = self.parse_job(raw, portal, categories)
                previous = jobs_by_id.get(record.external_id)
                if previous is not None:
                    if previous.content_hash() != record.content_hash():
                        raise RuntimeError(
                            f"Bilibili returned conflicting job {record.external_id}"
                        )
                    raise RuntimeError(f"Bilibili repeated job {record.external_id}")
                jobs_by_id[record.external_id] = record
            if page_number < first["pages"]:
                payload = await self._next_page(page_number + 1)

        if complete and len(jobs_by_id) != first["total"]:
            raise RuntimeError(
                "Bilibili pagination count mismatch: "
                f"declared={first['total']}, unique={len(jobs_by_id)}"
            )

        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts={"all": first["total"]},
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_first_page(
        self,
        portal: PortalConfig,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        drain_json_responses(self._category_responses)
        self._active_endpoint_group = portal.endpoint_group
        await self.page.goto(
            portal.page_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        payload = await self._next_position_payload(
            f"positions:{portal.channel.value}:1"
        )
        category_payload = await next_json_payload(
            self._category_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Bilibili categories:{portal.channel.value}",
        )
        if category_payload.get("code") != 0 or not isinstance(
            category_payload.get("data"), list
        ):
            message = json.dumps(category_payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Bilibili category response: {message}")
        self._save_categories(portal.channel, category_payload)
        return payload, category_payload

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.locator(".ant-pagination-next")
        if await next_button.count() != 1:
            raise RuntimeError("Bilibili pagination has no unique next button")
        classes = await next_button.get_attribute("class") or ""
        aria_disabled = await next_button.get_attribute("aria-disabled")
        if "ant-pagination-disabled" in classes or aria_disabled == "true":
            raise RuntimeError(f"Bilibili pagination ended before page {page_number}")
        await next_button.click()
        return await self._next_position_payload(f"positions:{page_number}")

    async def _next_position_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Bilibili response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Bilibili position response: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    @staticmethod
    def _position_page(payload: dict[str, Any], *, expected_page: int) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Bilibili response data is not an object")
        rows = data.get("list")
        total = _response_int(data.get("total"), "total", allow_zero=True)
        pages = _response_int(data.get("pages"), "pages", allow_zero=True)
        page_size = _response_int(data.get("size"), "size")
        if rows is None and total == 0:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError("Bilibili response has no job list")
        if pages != (total + page_size - 1) // page_size:
            raise RuntimeError(
                "Bilibili pagination metadata is inconsistent: "
                f"pages={pages}, total={total}, size={page_size}"
            )
        expected_rows = min(page_size, max(total - (expected_page - 1) * page_size, 0))
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Bilibili page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"Bilibili page {expected_page} contains a non-object row"
                )
        return {"rows": rows, "total": total, "pages": pages}

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".ant-pagination-item-active")
        if await active.count() != 1:
            raise RuntimeError("Bilibili pagination has no unique active page")
        text = (await active.inner_text()).strip()
        if text != str(expected_page):
            raise RuntimeError(
                f"Bilibili page mismatch: expected={expected_page}, got={text!r}"
            )

    @staticmethod
    def parse_category_tree(payload: dict[str, Any]) -> dict[str, str]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Bilibili category tree is not a list")
        categories: dict[str, str] = {}
        for root in data:
            if not isinstance(root, dict):
                raise RuntimeError("Bilibili category tree contains a non-object root")
            children = root.get("sonRankBasics")
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    raise RuntimeError("Bilibili category tree contains a non-object item")
                code = _optional(child.get("rankCode"))
                name = _optional(child.get("rankName"))
                if code is None or name is None:
                    raise RuntimeError("Bilibili category tree has an unnamed item")
                if name in categories and categories[name] != code:
                    raise RuntimeError(f"Bilibili category name {name!r} is ambiguous")
                categories[name] = code
        if not categories:
            raise RuntimeError("Bilibili category tree exposed no top-level categories")
        return categories

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        portal: PortalConfig,
        categories: dict[str, str],
    ) -> JobRecord:
        external_id = _required_id(raw.get("id"))
        title = _required(raw, "positionName", f"Bilibili job title ({external_id})")
        combined = _required(
            raw,
            "positionDescription",
            f"Bilibili job description ({external_id})",
        )
        description, requirements = _split_description(combined, external_id)
        employment_type = _required(
            raw,
            "positionTypeName",
            f"Bilibili job type ({external_id})",
        )
        if employment_type != portal.employment_type_name:
            raise ValueError(
                f"Bilibili job {external_id} has type {employment_type!r}; "
                f"expected {portal.employment_type_name!r}"
            )
        category_name = _required(
            raw,
            "postCodeName",
            f"Bilibili job category ({external_id})",
        )
        category_code = categories.get(category_name)
        if category_code is None:
            raise ValueError(
                f"Bilibili job {external_id} has unknown category {category_name!r}"
            )

        return JobRecord(
            source_key="bilibili_cn",
            external_id=external_id,
            source_url=(
                f"https://jobs.bilibili.com/{portal.path}/positions/{external_id}"
            ),
            company_name="哔哩哔哩",
            channel=portal.channel,
            employment_type_id=portal.work_type_code,
            employment_type_name=employment_type,
            recruitment_project_id=_optional_id(raw.get("campusProjectId")),
            title=title,
            description=description,
            requirements=requirements,
            published_at=_source_datetime(raw.get("pushTime"), "pushTime"),
            is_hot=_zero_one_bool(raw.get("hotRecruit"), "hotRecruit"),
            locations=_locations(raw.get("workLocation"), external_id),
            categories=[
                SourceCategoryRecord(
                    external_id=category_code,
                    name=category_name,
                    assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                )
            ],
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        position_path = (
            f"/api/{self._active_endpoint_group}/position/positionList"
            if self._active_endpoint_group is not None
            else None
        )
        if position_path is not None and position_path in response.url:
            enqueue_json_response(self._position_responses, response)
        elif "/api/campus/position/postCodeList" in response.url:
            enqueue_json_response(self._category_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.bilibili_request_delay_seconds - (
            asyncio.get_running_loop().time() - self._last_request_at
        )
        if delay > 0:
            await asyncio.sleep(delay)

    def _save_payload(
        self,
        channel: Channel,
        page_number: int,
        payload: dict[str, Any],
    ) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="all",
                    offset=page_number - 1,
                    payload=payload,
                )
            )

    def _save_categories(self, channel: Channel, payload: dict[str, Any]) -> None:
        if self.raw_store is not None:
            self.snapshots.append(
                self.raw_store.save(
                    channel=channel,
                    partition="category-tree",
                    offset=0,
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


def _required_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Bilibili job id is invalid: {value!r}")
    text = str(value).strip()
    if not text or (isinstance(value, int) and value < 1):
        raise ValueError(f"Bilibili job id is invalid: {value!r}")
    return text


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Bilibili project id is invalid: {value!r}")
    text = str(value).strip()
    if not text or (isinstance(value, int) and value < 0):
        raise ValueError(f"Bilibili project id is invalid: {value!r}")
    return text


def _response_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"Bilibili {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Bilibili {field} is not an integer: {value!r}")
    return value


def _source_datetime(value: Any, field: str) -> datetime | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Bilibili {field} is not a source datetime: {text!r}") from exc
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))


def _zero_one_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    raise ValueError(f"Bilibili {field} is not 0 or 1: {value!r}")


def _locations(value: Any, external_id: str) -> list[LocationRecord]:
    text = _optional(value)
    if text is None:
        raise ValueError(f"Bilibili job {external_id} has no work location")
    names = [item.strip() for item in re.split(r"[,，/]", text) if item.strip()]
    names = list(dict.fromkeys(names))
    if not names:
        raise ValueError(f"Bilibili job {external_id} has no work location")
    return [LocationRecord(code=f"city:{name}", name=name) for name in names]


def _split_description(value: str, external_id: str) -> tuple[str, str]:
    separator = re.search(
        r"(?:^|\n)\s*(?:工作要求|岗位要求|任职要求|职位要求)\s*[:：]\s*",
        value,
    )
    if separator is None:
        raise ValueError(
            f"Bilibili job {external_id} has no explicit requirement heading"
        )
    description = value[: separator.start()].strip()
    requirements = value[separator.end() :].strip()
    if not description or not requirements:
        raise ValueError(f"Bilibili job {external_id} has an empty text section")
    return description, requirements
