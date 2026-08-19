import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.bilibili import PORTALS, BilibiliConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "bilibili_job.json"
CATEGORIES = {"技术类": "01", "产品运营类": "02"}


def test_bilibili_job_preserves_source_fields_and_explicit_text_sections() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = BilibiliConnector.parse_job(
        raw,
        PORTALS[Channel.CAMPUS],
        CATEGORIES,
    )

    assert record.source_key == "bilibili_cn"
    assert record.external_id == "900002"
    assert record.employment_type_id == "3"
    assert record.employment_type_name == "全职"
    assert record.recruitment_project_id == "55"
    assert record.description.endswith("负责示例系统的设计与开发。")
    assert record.requirements == "1. 具备扎实的工程基础。"
    assert record.published_at == datetime(
        2026,
        4,
        13,
        9,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.is_hot is True
    assert [item.name for item in record.locations] == ["上海", "北京"]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("01", "技术类")
    ]
    assert str(record.source_url).endswith("/campus/positions/900002")
    assert record.source_payload == raw


def test_bilibili_internship_rejects_full_time_job() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="expected '实习'"):
        BilibiliConnector.parse_job(
            raw,
            PORTALS[Channel.INTERNSHIP],
            CATEGORIES,
        )


def test_bilibili_requires_explicit_requirement_heading() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["positionDescription"] = "只有一段无法可靠拆分的文本"

    with pytest.raises(ValueError, match="no explicit requirement heading"):
        BilibiliConnector.parse_job(
            raw,
            PORTALS[Channel.CAMPUS],
            CATEGORIES,
        )


def test_bilibili_category_tree_keeps_official_codes() -> None:
    payload = {
        "data": [
            {
                "rankCode": "0",
                "rankName": "B站职位",
                "sonRankBasics": [
                    {"rankCode": "01", "rankName": "技术类"},
                    {"rankCode": "02", "rankName": "产品运营类"},
                ],
            }
        ]
    }

    assert BilibiliConnector.parse_category_tree(payload) == CATEGORIES


def test_bilibili_validates_final_page_size() -> None:
    payload = {
        "data": {
            "list": [{}],
            "pages": 2,
            "size": 10,
            "total": 11,
        }
    }

    page = BilibiliConnector._position_page(payload, expected_page=2)

    assert page["total"] == 11
    assert len(page["rows"]) == 1
