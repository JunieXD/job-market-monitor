"""Alibaba Cloud public experienced-recruitment connector."""

from math import ceil
from typing import Any

from playwright.async_api import Response

from job_market.connectors.browser_json import (
    drain_json_responses,
    enqueue_json_response,
)
from job_market.connectors.cainiao import (
    MAX_COLLECTION_ATTEMPTS,
    UI_PAGE_SIZE,
    CainiaoConnector,
    _optional,
    _response_int,
)
from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionResult,
    JobRecord,
    SourceCategoryRecord,
)

POSITION_ENDPOINT = "/position/search"


class AliyunConnector(CainiaoConnector):
    """Collect Alibaba Cloud's root list and official top-level categories."""

    source_key = "alibaba_cloud_social"
    company_name = "阿里云"
    portal_name = "Alibaba Cloud"
    position_page_url = (
        "https://careers.aliyun.com/off-campus/position-list?lang=zh"
    )
    position_url = (
        "https://careers.aliyun.com/off-campus/position-detail"
        "?positionId={external_id}"
    )
    request_delay_setting = "aliyun_request_delay_seconds"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._active_filter_kind: str | None = None
        self._active_category_name: str | None = None
        self._active_location_name: str | None = None
        self._observed_category_filters: dict[str, tuple[str, str]] = {}
        self._observed_location_filters: dict[str, str] = {}

    async def collect(
        self,
        channel: Channel,
        *,
        max_pages: int | None = None,
    ) -> CollectionResult:
        if channel is not Channel.EXPERIENCED:
            raise ValueError("Alibaba Cloud connector supports only experienced jobs")

        initial_payload = await self._open_root()
        first = self._position_page(initial_payload, expected_page=1)
        root_total = first["total"]
        root_sample = {
            record.external_id: record
            for record in (self.parse_job(raw) for raw in first["rows"])
        }
        self.pages_fetched += 1
        self._save_payload(channel, "root-sample", 0, initial_payload)
        partition_counts = {
            "all": root_total,
            "root-initial": root_total,
            "root-sample": len(root_sample),
        }
        if max_pages is not None and self.pages_fetched >= max_pages:
            return CollectionResult(
                channel=channel,
                jobs=list(root_sample.values()),
                snapshots=self.snapshots,
                partition_counts=partition_counts,
                pages_fetched=self.pages_fetched,
                complete=False,
            )

        jobs_by_id: dict[str, JobRecord] = dict(root_sample)
        root_window_pages = min(ceil(root_total / UI_PAGE_SIZE), 50)
        payload = initial_payload
        for page_number in range(2, root_window_pages + 1):
            if max_pages is not None and self.pages_fetched >= max_pages:
                partition_counts["root-window"] = len(jobs_by_id)
                return CollectionResult(
                    channel=channel,
                    jobs=list(jobs_by_id.values()),
                    snapshots=self.snapshots,
                    partition_counts=partition_counts,
                    pages_fetched=self.pages_fetched,
                    complete=False,
                )
            payload = await self._next_page(page_number)
            current = self._position_page(payload, expected_page=page_number)
            if current["total"] != root_total:
                raise RuntimeError(
                    "Alibaba Cloud root total changed while collecting its "
                    f"safe 500-row window: {root_total} -> {current['total']}"
                )
            self.pages_fetched += 1
            self._save_payload(
                channel,
                "root-window",
                page_number - 1,
                payload,
            )
            self._merge_records(
                jobs_by_id,
                {
                    record.external_id: record
                    for record in (self.parse_job(raw) for raw in current["rows"])
                },
                context="root window",
            )
        partition_counts["root-window"] = len(jobs_by_id)

        category_catalog = await self._category_catalog()
        location_catalog = await self._location_catalog()

        for location_name in location_catalog:
            records, total, complete = await self._collect_filter_records(
                "location",
                location_name,
                max_pages,
            )
            location_code = self._observed_location_filters.get(location_name)
            if location_code is None:
                raise RuntimeError(
                    f"Alibaba Cloud location {location_name!r} exposed no request identity"
                )
            partition_counts[f"location:{location_code}"] = total
            self._merge_records(
                jobs_by_id,
                records,
                context=f"location {location_name!r}",
            )
            if not complete:
                return CollectionResult(
                    channel=channel,
                    jobs=list(jobs_by_id.values()),
                    snapshots=self.snapshots,
                    partition_counts=partition_counts,
                    pages_fetched=self.pages_fetched,
                    complete=False,
                )

        partition_counts["location-covered-union"] = len(jobs_by_id)

        assignments: dict[str, list[SourceCategoryRecord]] = {
            external_id: list(record.categories)
            for external_id, record in jobs_by_id.items()
        }

        for category_name in category_catalog:
            records, total, complete = await self._collect_filter_records(
                "category",
                category_name,
                max_pages,
            )
            filter_values = self._observed_category_filters.get(category_name)
            if filter_values is None:
                raise RuntimeError(
                    f"Alibaba Cloud category {category_name!r} exposed no request identity"
                )
            category_id, _subcategory_ids = filter_values
            partition_counts[f"category:{category_id}"] = total
            category = SourceCategoryRecord(
                external_id=f"top:{category_id}",
                name=category_name,
                assignment_method=CategoryAssignmentMethod.FILTER_MEMBERSHIP,
            )
            for external_id, record in records.items():
                previous = jobs_by_id.get(external_id)
                if previous is None:
                    jobs_by_id[external_id] = record
                    assignments[external_id] = list(record.categories)
                elif previous.content_hash() != record.content_hash():
                    raise RuntimeError(
                        f"Alibaba Cloud job {external_id} changed across categories"
                    )
                current = assignments[external_id]
                if category.external_id not in {
                    item.external_id for item in current
                }:
                    current.append(category)
            if not complete:
                return CollectionResult(
                    channel=channel,
                    jobs=self._with_categories(jobs_by_id, assignments),
                    snapshots=self.snapshots,
                    partition_counts=partition_counts,
                    pages_fetched=self.pages_fetched,
                    complete=False,
                )

        if len(jobs_by_id) != root_total:
            raise RuntimeError(
                "Alibaba Cloud safe partition union does not cover the root total: "
                f"root={root_total}, partition-union={len(jobs_by_id)}"
            )

        self._save_payload(
            channel,
            "category-catalog",
            0,
            {
                "categories": [
                    {
                        "name": name,
                        "category": self._observed_category_filters[name][0],
                        "subCategories": self._observed_category_filters[name][1],
                    }
                    for name in category_catalog
                ]
            },
        )
        self._save_payload(
            channel,
            "location-catalog",
            0,
            {
                "locations": [
                    {
                        "name": name,
                        "regions": self._observed_location_filters[name],
                    }
                    for name in location_catalog
                ]
            },
        )
        partition_counts["category-catalog"] = len(category_catalog)
        partition_counts["location-catalog"] = len(location_catalog)
        partition_counts["category-covered-unique"] = sum(
            bool(categories) for categories in assignments.values()
        )
        partition_counts["collected-unique"] = len(jobs_by_id)
        return CollectionResult(
            channel=channel,
            jobs=self._with_categories(jobs_by_id, assignments),
            snapshots=self.snapshots,
            partition_counts=partition_counts,
            pages_fetched=self.pages_fetched,
            complete=True,
        )

    async def _category_catalog(self) -> list[str]:
        trees = self.page.locator('[role="tree"]')
        if await trees.count() < 2:
            raise RuntimeError("Alibaba Cloud category tree is missing")
        return await self._tree_labels(trees.nth(0), "category")

    async def _location_catalog(self) -> list[str]:
        trees = self.page.locator('[role="tree"]')
        if await trees.count() < 2:
            raise RuntimeError("Alibaba Cloud location tree is missing")
        return await self._tree_labels(trees.nth(1), "location")

    @staticmethod
    async def _tree_labels(tree: Any, label: str) -> list[str]:
        nodes = tree.locator('[role="treeitem"][aria-level="1"]')
        names: list[str] = []
        for index in range(await nodes.count()):
            name = _optional(await nodes.nth(index).get_attribute("aria-label"))
            if name is None:
                raise RuntimeError(f"Alibaba Cloud {label} has no label")
            names.append(name)
        if not names or len(names) != len(set(names)):
            raise RuntimeError(
                f"Alibaba Cloud {label} catalog is empty or duplicated"
            )
        return names

    async def _collect_filter_records(
        self,
        filter_kind: str,
        filter_name: str,
        max_pages: int | None,
    ) -> tuple[dict[str, JobRecord], int, bool]:
        union: dict[str, JobRecord] = {}
        target_total: int | None = None

        for attempt in range(1, MAX_COLLECTION_ATTEMPTS + 1):
            payload = await self._open_filter(filter_kind, filter_name)
            first = self._position_page(payload, expected_page=1)
            if target_total != first["total"]:
                target_total = first["total"]
                union = {}
            if target_total > 500:
                raise RuntimeError(
                    f"Alibaba Cloud {filter_kind} {filter_name!r} exceeds the "
                    f"500-row deep-pagination limit: {target_total}"
                )
            total_pages = ceil(target_total / UI_PAGE_SIZE) if target_total else 0
            current_pass: dict[str, JobRecord] = {}
            label = f"{filter_kind}-{filter_name}-retry-{attempt}"

            for page_number in range(1, total_pages + 1):
                if max_pages is not None and self.pages_fetched >= max_pages:
                    return union or current_pass, target_total, False
                current = self._position_page(payload, expected_page=page_number)
                if current["total"] != target_total:
                    break
                self.pages_fetched += 1
                self._save_payload(
                    channel=Channel.EXPERIENCED,
                    partition=label,
                    offset=page_number - 1,
                    payload=payload,
                )
                for raw in current["rows"]:
                    record = self.parse_job(raw)
                    previous = current_pass.get(record.external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Alibaba Cloud job {record.external_id} changed within "
                            f"{filter_kind} {filter_name!r}"
                        )
                    current_pass[record.external_id] = record
                if page_number < total_pages:
                    payload = await self._next_page(page_number + 1)
            else:
                for external_id, record in current_pass.items():
                    previous = union.get(external_id)
                    if previous is not None and (
                        previous.content_hash() != record.content_hash()
                    ):
                        raise RuntimeError(
                            f"Alibaba Cloud job {external_id} changed during "
                            f"{filter_kind} {filter_name!r} retries"
                        )
                    union[external_id] = record
                if len(union) == target_total:
                    return union, target_total, True
                if len(union) > target_total:
                    raise RuntimeError(
                        f"Alibaba Cloud {filter_kind} {filter_name!r} exceeded its "
                        f"declared total: {len(union)} > {target_total}"
                    )

        raise RuntimeError(
            f"Alibaba Cloud {filter_kind} {filter_name!r} did not converge: "
            f"declared={target_total}, unique={len(union)}"
        )

    async def _open_filter(
        self,
        filter_kind: str,
        filter_name: str,
    ) -> dict[str, Any]:
        if filter_kind not in {"category", "location"}:
            raise ValueError(f"Unsupported Alibaba Cloud filter: {filter_kind}")
        await self._open_root()
        self._active_filter_kind = filter_kind
        self._active_category_name = (
            filter_name if filter_kind == "category" else None
        )
        self._active_location_name = (
            filter_name if filter_kind == "location" else None
        )
        self._active_page = 1
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        tree_index = 0 if filter_kind == "category" else 1
        tree = self.page.locator('[role="tree"]').nth(tree_index)
        item = tree.locator(
            f'[role="treeitem"][aria-level="1"][aria-label="{filter_name}"]'
        )
        if await item.count() != 1:
            raise RuntimeError(
                f"Alibaba Cloud {filter_kind} {filter_name!r} is not unique"
            )
        await item.locator("label.next-checkbox-wrapper").click()
        payload = await self._next_payload(f"{filter_kind}:{filter_name}:1")
        return payload

    async def _open_root(self) -> dict[str, Any]:
        self._active_filter_kind = None
        self._active_category_name = None
        self._active_location_name = None
        return await super()._open_root()

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        await self._rate_limit()
        drain_json_responses(self._position_responses)
        self._active_page = page_number
        next_button = self.page.locator('button[aria-label^="下一页，当前第"]')
        if await next_button.count() != 1:
            raise RuntimeError("Alibaba Cloud pagination has no unique next button")
        if await next_button.is_disabled():
            raise RuntimeError(
                f"Alibaba Cloud pagination ended before page {page_number}"
            )
        await next_button.evaluate("element => element.click()")
        payload = await self._next_payload(f"positions:{page_number}")
        await self._assert_active_page(page_number)
        return payload

    async def _assert_active_page(self, expected_page: int) -> None:
        selector = f'button[aria-label="下一页，当前第{expected_page}页"]'
        await self.page.locator(selector).wait_for(
            state="attached",
            timeout=30_000,
        )

    @classmethod
    def _position_page(
        cls,
        payload: dict[str, Any],
        *,
        expected_page: int,
    ) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise RuntimeError("Alibaba Cloud position response has no content")
        total = _response_int(content.get("totalCount"), "totalCount", allow_zero=True)
        rows = content.get("datas")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError("Alibaba Cloud position response has invalid datas")

        page_number = _response_int(content.get("currentPage"), "currentPage")
        page_size = _response_int(content.get("pageSize"), "pageSize")
        metadata_is_correct = (
            page_number == expected_page and page_size == UI_PAGE_SIZE
        )
        metadata_is_known_broken = page_number == 1 and page_size == 500
        if not metadata_is_correct and not metadata_is_known_broken:
            raise RuntimeError(
                "Alibaba Cloud pagination metadata changed: "
                f"currentPage={page_number}, pageSize={page_size}"
            )
        expected_rows = min(
            UI_PAGE_SIZE,
            max(total - (expected_page - 1) * UI_PAGE_SIZE, 0),
        )
        if len(rows) != expected_rows:
            raise RuntimeError(
                f"Alibaba Cloud page {expected_page} row mismatch: "
                f"expected={expected_rows}, got={len(rows)}"
            )
        return {"rows": rows, "total": total}

    @classmethod
    def parse_job(cls, raw: dict[str, Any]) -> JobRecord:
        record = super().parse_job(raw)
        category_name = _optional(raw.get("categoryName"))
        if category_name is None:
            return record
        category_type = _optional(raw.get("categoryType"))
        direct = SourceCategoryRecord(
            external_id=(
                f"category:{category_type}"
                if category_type is not None
                else f"label:{category_name}"
            ),
            name=category_name,
            assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
        )
        existing_names = {category.name for category in record.categories}
        if existing_names and category_name not in existing_names:
            raise ValueError(
                "Alibaba Cloud categoryName conflicts with categories: "
                f"{category_name!r} != {sorted(existing_names)!r}"
            )
        if direct.external_id in {
            category.external_id for category in record.categories
        }:
            return record
        return record.model_copy(
            update={"categories": [*record.categories, direct]}
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
        expected = {
            "channel": "group_official_site",
            "language": "zh",
            "batchId": "",
            "categories": "",
            "deptCodes": [],
            "key": "",
            "pageIndex": self._active_page,
            "pageSize": UI_PAGE_SIZE,
            "regions": "",
            "subCategories": "",
            "shareType": "",
            "shareId": "",
            "myReferralShareCode": "",
        }
        if self._active_filter_kind is None:
            if post_data == expected:
                enqueue_json_response(self._position_responses, response)
            return

        if self._active_filter_kind == "location":
            regions = _optional(post_data.get("regions"))
            expected["regions"] = regions
            if regions is None or post_data != expected:
                return
            assert self._active_location_name is not None
            previous_region = self._observed_location_filters.get(
                self._active_location_name
            )
            if previous_region is not None and previous_region != regions:
                return
            self._observed_location_filters[self._active_location_name] = regions
            enqueue_json_response(self._position_responses, response)
            return

        categories = _optional(post_data.get("categories"))
        subcategories = _optional(post_data.get("subCategories"))
        expected["categories"] = categories
        expected["subCategories"] = subcategories
        if categories is None or subcategories is None or post_data != expected:
            return
        assert self._active_category_name is not None
        previous = self._observed_category_filters.get(self._active_category_name)
        current = (categories, subcategories)
        if previous is not None and previous != current:
            return
        self._observed_category_filters[self._active_category_name] = current
        enqueue_json_response(self._position_responses, response)

    @staticmethod
    def _merge_records(
        jobs_by_id: dict[str, JobRecord],
        records: dict[str, JobRecord],
        *,
        context: str,
    ) -> None:
        for external_id, record in records.items():
            previous = jobs_by_id.get(external_id)
            if previous is not None and previous.content_hash() != record.content_hash():
                raise RuntimeError(
                    f"Alibaba Cloud job {external_id} changed across {context}"
                )
            jobs_by_id[external_id] = record

    @staticmethod
    def _with_categories(
        jobs_by_id: dict[str, JobRecord],
        assignments: dict[str, list[SourceCategoryRecord]],
    ) -> list[JobRecord]:
        return [
            job.model_copy(
                update={
                    "categories": sorted(
                        assignments[external_id],
                        key=lambda item: (item.name, item.external_id),
                    )
                }
            )
            for external_id, job in jobs_by_id.items()
        ]
