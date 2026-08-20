"""Lenovo China public campus-recruitment connector."""

import asyncio
import json
import re
from html.parser import HTMLParser
from math import ceil
from typing import Any
from urllib.parse import urlencode

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
PAGE_SIZE = 10
POSITION_LIST_URL = "https://talent.lenovo.com.cn/position"
POSITION_ENDPOINT = "/gateway/jobBase/list"
DICTIONARY_ENDPOINT = "/gateway/sysDict/all"
PROJECT_TYPES = (1, 3)


class LenovoConnector:
    """Collect active graduate and named-talent projects from Lenovo China."""

    source_key = "lenovo_campus_cn"

    def __init__(self, page: Page, settings: Settings, raw_store: RawStore | None = None):
        self.page = page
        self.settings = settings
        self.raw_store = raw_store
        self.snapshots: list[RawSnapshotRecord] = []
        self.pages_fetched = 0
        self._last_request_at = 0.0
        self._position_responses: JsonResponseQueue = asyncio.Queue()
        self._dictionary_responses: JsonResponseQueue = asyncio.Queue()
        self._active_page = 1
        self._active_project_type = PROJECT_TYPES[0]
        self.page.on("response", self._record_response)

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.CAMPUS:
            raise ValueError("Lenovo connector supports only campus jobs")

        dictionaries: dict[str, dict[str, str]] | None = None
        jobs_by_id: dict[str, JobRecord] = {}
        partition_counts: dict[str, int] = {}
        complete = True

        for project_type in PROJECT_TYPES:
            payload = await self._open_partition(project_type)
            if dictionaries is None:
                dictionary_payload = await self._next_dictionary()
                dictionaries = self._parse_dictionaries(dictionary_payload)
                self._validate_projects(dictionaries["projectType"])
                self._save_payload(
                    channel,
                    "dictionaries",
                    0,
                    dictionary_payload,
                )

            first = self._position_page(payload, expected_page=1)
            total = first["total"]
            pages = ceil(total / PAGE_SIZE) if total else 0
            project_name = dictionaries["projectType"][str(project_type)]
            partition_counts[f"project:{project_type}"] = total

            for page_number in range(1, pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    complete = False
                    break
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != total:
                    raise RuntimeError(
                        f"Lenovo project {project_type} total changed during collection"
                    )
                self.pages_fetched += 1
                self._save_payload(
                    channel,
                    f"project-{project_type}",
                    page_number - 1,
                    payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(
                        raw,
                        project_name=project_name,
                        city_names=dictionaries["city_portal"],
                        degree_names=dictionaries["education"],
                    )
                    previous = jobs_by_id.get(record.external_id)
                    if previous is not None:
                        raise RuntimeError(
                            "Lenovo returned the same job in multiple project partitions: "
                            f"{record.external_id}"
                        )
                    jobs_by_id[record.external_id] = record
                if page_number < pages:
                    payload = await self._next_page(page_number + 1)
            if not complete:
                break

        expected_total = sum(
            count
            for key, count in partition_counts.items()
            if key.startswith("project:")
        )
        if complete and len(jobs_by_id) != expected_total:
            raise RuntimeError(
                "Lenovo partition count mismatch: "
                f"declared={expected_total}, unique={len(jobs_by_id)}"
            )
        partition_counts["all"] = expected_total
        return CollectionResult(
            channel=channel,
            jobs=list(jobs_by_id.values()),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=complete,
        )

    async def _open_partition(self, project_type: int) -> dict[str, Any]:
        self._active_project_type = project_type
        self._active_page = 1
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        drain_json_responses(self._dictionary_responses)
        await self.page.goto(
            f"{POSITION_LIST_URL}?projectType={project_type}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        return await self._next_payload(f"project:{project_type}:1")

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        self._active_page = page_number
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        next_button = self.page.locator(".el-pagination button.btn-next")
        if await next_button.count() != 1:
            raise RuntimeError("Lenovo pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(f"Lenovo pagination ended before page {page_number}")
        await next_button.click()
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        active = self.page.locator(".el-pager li.is-active")
        deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await active.count() == 1:
                text = (await active.inner_text()).strip()
                if text == str(expected_page):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"Lenovo page did not render active page {expected_page}")

    async def _next_payload(self, operation: str) -> dict[str, Any]:
        payload = await next_json_payload(
            self._position_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation=f"Lenovo response: {operation}",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("result"), dict):
            message = json.dumps(payload, ensure_ascii=False)[:1000]
            raise RuntimeError(f"Invalid Lenovo response for {operation}: {message}")
        self._last_request_at = asyncio.get_running_loop().time()
        return payload

    async def _next_dictionary(self) -> dict[str, Any]:
        payload = await next_json_payload(
            self._dictionary_responses,
            timeout_seconds=RESPONSE_TIMEOUT_SECONDS,
            operation="Lenovo response: dictionaries",
        )
        if payload.get("code") != 0 or not isinstance(payload.get("result"), list):
            raise RuntimeError("Lenovo dictionary response is invalid")
        return payload

    @staticmethod
    def _position_page(
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Lenovo response has no result object")
        total = _response_int(result.get("total"), "total", allow_zero=True)
        rows = result.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Lenovo response has invalid rows")
        expected_rows = min(
            PAGE_SIZE,
            max(total - (expected_page - 1) * PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Lenovo page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @staticmethod
    def _parse_dictionaries(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise RuntimeError("Lenovo dictionary result is not a list")
        wanted = {"city_portal", "education", "projectType"}
        result: dict[str, dict[str, str]] = {}
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("dictCode") not in wanted:
                continue
            code = str(raw["dictCode"])
            children = raw.get("children")
            if not isinstance(children, list):
                raise RuntimeError(f"Lenovo dictionary {code} has no children")
            values: dict[str, str] = {}
            for child in children:
                if not isinstance(child, dict):
                    raise RuntimeError(f"Lenovo dictionary {code} has invalid child")
                value = _required(child, "dictValue", f"Lenovo {code} value")
                name = _required(child, "dictName", f"Lenovo {code} name ({value})")
                if value in values:
                    raise RuntimeError(f"Lenovo dictionary {code} repeats {value}")
                values[value] = name
            result[code] = values
        if set(result) != wanted:
            raise RuntimeError(
                f"Lenovo dictionaries are incomplete: {sorted(result)}"
            )
        return result

    @staticmethod
    def _validate_projects(projects: dict[str, str]) -> None:
        expected = {"1": "应届生招聘", "3": "人才项目"}
        actual = {key: projects.get(key) for key in expected}
        if actual != expected:
            raise RuntimeError(f"Lenovo project labels changed: {actual!r}")

    @staticmethod
    def parse_job(
        raw: dict[str, Any],
        *,
        project_name: str,
        city_names: dict[str, str],
        degree_names: dict[str, str],
    ) -> JobRecord:
        external_id = _required(raw, "id", "Lenovo job id")
        title = _required(raw, "jobName", f"Lenovo job title ({external_id})")
        project_type = _required(
            raw,
            "projectType",
            f"Lenovo project type ({external_id})",
        )
        category_name = _optional(raw.get("typeName"))
        degree_code = _optional(raw.get("educationRequired"))
        if degree_code is not None and degree_code not in degree_names:
            raise ValueError(
                f"Lenovo job {external_id} has unknown degree {degree_code!r}"
            )
        location_codes = _csv(raw.get("workPlace"))
        locations: list[LocationRecord] = []
        for code in location_codes:
            try:
                name = city_names[code]
            except KeyError as exc:
                raise ValueError(
                    f"Lenovo job {external_id} has unknown city code {code!r}"
                ) from exc
            locations.append(LocationRecord(code=code, name=name))
        business_units = [
            BusinessUnitRecord(code=f"name:{name}", name=name)
            for name in _csv(raw.get("firstDeptId"))
        ]
        query = urlencode({"projectType": project_type, "jobId": external_id})
        return JobRecord(
            source_key="lenovo_campus_cn",
            external_id=external_id,
            source_url=f"{POSITION_LIST_URL}?{query}",
            company_name="联想集团",
            channel=Channel.CAMPUS,
            employment_type_id=project_type,
            employment_type_name=project_name,
            recruitment_project_id=project_type,
            recruitment_project_name=project_name,
            title=title,
            description=_plain_text(_optional(raw.get("jobDuties"))),
            requirements=_plain_text(_optional(raw.get("jobRequirement"))),
            degree_code=degree_code,
            degree_name=degree_names.get(degree_code),
            department_name=_optional(raw.get("firstDeptId")),
            is_hot=_binary_bool(raw.get("hotFlag"), "hotFlag"),
            locations=locations,
            categories=(
                []
                if category_name is None
                else [
                    SourceCategoryRecord(
                        external_id=f"label:{category_name}",
                        name=category_name,
                        assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
                    )
                ]
            ),
            business_units=business_units,
            source_payload=raw,
        )

    def _record_response(self, response: Response) -> None:
        if response.status != 200:
            return
        if DICTIONARY_ENDPOINT in response.url:
            enqueue_json_response(self._dictionary_responses, response)
            return
        if POSITION_ENDPOINT not in response.url:
            return
        params = response.request.url.split("?", 1)
        if len(params) != 2:
            return
        query = dict(
            item.split("=", 1)
            for item in params[1].split("&")
            if "=" in item
        )
        expected = {
            "projectType": str(self._active_project_type),
            "pageNum": str(self._active_page),
        }
        if query == expected:
            enqueue_json_response(self._position_responses, response)

    async def _rate_limit(self) -> None:
        delay = self.settings.lenovo_request_delay_seconds - (
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
        raise RuntimeError(f"Lenovo {field} is not an integer: {value!r}")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"Lenovo {field} is invalid: {value!r}")
    return value


def _csv(value: Any) -> list[str]:
    text = _optional(value)
    if text is None:
        return []
    return list(dict.fromkeys(item.strip() for item in text.split(",") if item.strip()))


def _binary_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if value not in (0, 1):
        raise ValueError(f"Lenovo {field} is not binary: {value!r}")
    return bool(value)


def _plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    parser = _SourceTextParser()
    parser.feed(value)
    parser.close()
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in "".join(parser.parts).replace("\r", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line) or None


class _SourceTextParser(HTMLParser):
    BLOCK_TAGS = {"br", "div", "li", "ol", "p", "section", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
