from typing import Any

import pytest

from job_market.connectors.bytedance import (
    API_CAP,
    PORTALS,
    ByteDanceConnector,
    Partition,
)
from job_market.schemas import Channel


class FakeConnector(ByteDanceConnector):
    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    async def _count_partition(self, portal: Any, partition: Partition) -> int:
        return self.counts[partition.label]


class FakePageStateConnector(ByteDanceConnector):
    def __init__(self) -> None:
        self.events: list[str] = []
        self._filters = {}
        self._partition_counts = {}
        self._current_partition = None
        self._current_payload = None
        self._active_portal = None

    async def _initialize_portal(self, portal: Any):
        self.events.append("initialize")
        return {"job_type_list": []}, _payload(10_000)

    async def _reset_to_root(self):
        if self._current_partition == Partition(label="all"):
            return self._current_payload
        self.events.append("reset")
        payload = _payload(10_000)
        self._set_current_partition(Partition(label="all"), payload)
        return payload

    async def _select_category(self, partition: Partition):
        self.events.append(f"category:{partition.category_name}")
        return _payload(5_000)

    async def _select_location(self, partition: Partition):
        self.events.append(f"location:{partition.location_name}")
        return _payload(500)


def _payload(count: int) -> dict[str, Any]:
    return {"code": 0, "data": {"count": count, "job_post_list": []}}


@pytest.mark.asyncio
async def test_any_channel_at_cap_splits_by_top_level_category() -> None:
    connector = FakeConnector(
        {
            "all": API_CAP,
            "category-研发": 6_000,
            "category-运营": 4_500,
        }
    )
    filters = {
        "job_type_list": [
            {"id": "rd", "name": "研发", "children": []},
            {"id": "ops", "name": "运营", "children": []},
        ],
        "city_list": [],
    }

    partitions = await connector._build_partitions(PORTALS[Channel.CAMPUS], filters)

    assert [partition.category_ids for partition in partitions] == [("rd",), ("ops",)]
    assert [partition.category_name for partition in partitions] == ["研发", "运营"]


@pytest.mark.asyncio
async def test_capped_category_splits_again_by_city() -> None:
    connector = FakeConnector(
        {
            "all": API_CAP,
            "category-研发": API_CAP,
            "category-研发-city-北京": 7_000,
            "category-研发-city-上海": 5_000,
        }
    )
    filters = {
        "job_type_list": [{"id": "rd", "name": "研发", "children": []}],
        "city_list": [
            {"code": "CT_11", "name": "北京"},
            {"code": "CT_125", "name": "上海"},
        ],
    }

    partitions = await connector._build_partitions(PORTALS[Channel.CAMPUS], filters)

    assert [partition.location_codes for partition in partitions] == [
        ("CT_11",),
        ("CT_125",),
    ]
    assert [partition.location_name for partition in partitions] == ["北京", "上海"]


@pytest.mark.asyncio
async def test_partition_switches_reuse_one_portal_and_reset_filters() -> None:
    connector = FakePageStateConnector()
    portal = PORTALS[Channel.EXPERIENCED]
    root = Partition(label="all")
    research = Partition(
        label="category-研发",
        category_ids=("rd",),
        category_name="研发",
    )
    research_beijing = Partition(
        label="category-研发-city-北京",
        category_ids=("rd",),
        location_codes=("beijing",),
        category_name="研发",
        location_name="北京",
    )

    await connector._open_partition(portal, root)
    await connector._open_partition(portal, research)
    await connector._open_partition(portal, research)
    await connector._open_partition(portal, research_beijing)

    assert connector.events == [
        "initialize",
        "category:研发",
        "reset",
        "category:研发",
        "location:北京",
    ]
    assert connector._partition_counts[root] == 10_000
    assert connector._partition_counts[research] == 5_000
    assert connector._partition_counts[research_beijing] == 500
