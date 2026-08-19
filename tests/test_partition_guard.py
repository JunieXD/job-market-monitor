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
