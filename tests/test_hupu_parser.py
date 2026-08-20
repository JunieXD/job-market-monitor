import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.hupu import PAGE_SIZE, PORTALS, HupuConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "hupu_job.json"


def test_hupu_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = HupuConnector.parse_job(raw, PORTALS[Channel.INTERNSHIP])

    assert record.source_key == "hupu_cn"
    assert record.external_id == "synthetic-hupu-001"
    assert record.external_code == "990001"
    assert record.channel is Channel.INTERNSHIP
    assert record.employment_type_id == "kind:实习"
    assert record.employment_type_name == "实习"
    assert record.description == "负责合成社区产品研发。"
    assert record.requirements == "具备扎实的编程基础。"
    assert record.published_at == datetime(
        2026,
        8,
        17,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.source_updated_at == datetime(
        2026,
        8,
        18,
        9,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.source_status == "1"
    assert record.department_code == "880001"
    assert record.department_name == "合成研发部门"
    assert record.is_hot is False
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:上海市·虹口区", "上海市·虹口区")
    ]
    assert record.source_payload == raw


def test_hupu_rejects_conflicting_source_dates() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["PostDate"] = "2026-08-18T00:00:00"

    with pytest.raises(ValueError, match="conflicting PostDate"):
        HupuConnector.parse_job(raw, PORTALS[Channel.INTERNSHIP])


def test_hupu_uses_count_instead_of_non_authoritative_total() -> None:
    payload = {
        "Count": 1,
        "Total": 0,
        "Data": [{"Id": "synthetic"}],
    }

    page = HupuConnector._position_page(payload, expected_page_index=0)

    assert page["total"] == 1
    assert page["rows"] == [{"Id": "synthetic"}]


async def test_hupu_rejects_duplicate_ids_across_pages() -> None:
    connector = HupuConnector(
        SimpleNamespace(),
        SimpleNamespace(hupu_request_delay_seconds=0.5),
    )
    first_rows = [{"Id": f"job-{index}"} for index in range(PAGE_SIZE)]
    connector._fetch_page = AsyncMock(
        side_effect=[
            {"Count": PAGE_SIZE + 1, "Data": first_rows},
            {"Count": PAGE_SIZE + 1, "Data": [{"Id": "job-0"}]},
        ]
    )

    with pytest.raises(RuntimeError, match="repeated job job-0"):
        await connector._collect_pass(
            PORTALS[Channel.INTERNSHIP],
            partition="test",
            max_pages=None,
        )
