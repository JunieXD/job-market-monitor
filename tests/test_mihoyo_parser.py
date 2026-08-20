import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_market.connectors.mihoyo import (
    LIST_PAGE_SIZE,
    PORTALS,
    MihoyoConnector,
)
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "mihoyo_job.json"


def test_mihoyo_job_preserves_list_and_detail_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = MihoyoConnector.parse_job(
        raw["list"],
        raw["detail"],
        PORTALS[Channel.CAMPUS],
    )

    assert record.source_key == "mihoyo_cn"
    assert record.external_id == "synthetic-mihoyo-001"
    assert record.external_code == "MHY-SYNTHETIC-001"
    assert record.channel is Channel.CAMPUS
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "全职"
    assert record.recruitment_project_id == "13"
    assert record.recruitment_project_name == "合成校园项目"
    assert record.description == "负责合成引擎工具研发。"
    assert record.requirements == (
        "具备扎实的软件工程基础。\n\n加分项：\n有图形工具经验。\n\n投递说明：\n请附合成作品说明。"
    )
    assert record.is_hot is True
    assert [(item.code, item.name) for item in record.locations] == [
        ("synthetic-shanghai", "上海市·徐汇区")
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("synthetic-tech", "技术")
    ]
    assert record.source_payload == raw


def test_mihoyo_rejects_list_detail_drift() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["detail"]["title"] = "另一个标题"

    with pytest.raises(ValueError, match="field title"):
        MihoyoConnector.parse_job(
            raw["list"],
            raw["detail"],
            PORTALS[Channel.CAMPUS],
        )


def test_mihoyo_rejects_detail_from_another_channel() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="does not match channel experienced"):
        MihoyoConnector.parse_job(
            raw["list"],
            raw["detail"],
            PORTALS[Channel.EXPERIENCED],
        )


def test_mihoyo_validates_final_page_shape() -> None:
    payload = {
        "data": {
            "list": [{"id": "last"}],
            "pageNo": 2,
            "pageSize": LIST_PAGE_SIZE,
            "total": LIST_PAGE_SIZE + 1,
        }
    }

    page = MihoyoConnector._list_page(payload, expected_page=2)

    assert page["total"] == LIST_PAGE_SIZE + 1
    assert page["rows"] == [{"id": "last"}]


async def test_mihoyo_rejects_duplicate_ids_across_pages() -> None:
    connector = MihoyoConnector(
        SimpleNamespace(),
        SimpleNamespace(mihoyo_request_delay_seconds=0.5),
    )
    first_rows = [{"id": f"job-{index}"} for index in range(LIST_PAGE_SIZE)]
    connector._fetch_list_page = AsyncMock(
        side_effect=[
            {
                "data": {
                    "list": first_rows,
                    "pageNo": 1,
                    "pageSize": LIST_PAGE_SIZE,
                    "total": LIST_PAGE_SIZE + 1,
                }
            },
            {
                "data": {
                    "list": [{"id": "job-0"}],
                    "pageNo": 2,
                    "pageSize": LIST_PAGE_SIZE,
                    "total": LIST_PAGE_SIZE + 1,
                }
            },
        ]
    )

    with pytest.raises(RuntimeError, match="repeated job job-0"):
        await connector._collect_list_pass(
            PORTALS[Channel.CAMPUS],
            partition="test",
            max_pages=None,
        )
