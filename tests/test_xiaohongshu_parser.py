import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.xiaohongshu import XiaohongshuConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "xiaohongshu_job.json"


def test_xiaohongshu_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = XiaohongshuConnector.parse_job(raw)

    assert record.source_key == "xiaohongshu_social_cn"
    assert record.external_id == "999001"
    assert record.channel is Channel.EXPERIENCED
    assert record.description == "负责合成智能体平台研发。"
    assert record.requirements == "具备扎实的软件工程能力。"
    assert record.recruitment_count == 3
    assert record.source_status == "in_recruitment"
    assert record.published_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [(item.code, item.name) for item in record.locations] == [
        ("1100", "北京市"),
        ("3100", "上海市"),
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:后端开发", "后端开发")
    ]
    assert record.source_payload == raw


def test_xiaohongshu_keeps_missing_headcount_null() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["amountInNeed"] = None

    record = XiaohongshuConnector.parse_job(raw)

    assert record.recruitment_count is None


def test_xiaohongshu_rejects_misaligned_locations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["workplace"] = "北京市"

    with pytest.raises(ValueError, match="inconsistent locations"):
        XiaohongshuConnector.parse_job(raw)


def test_xiaohongshu_validates_final_page_shape() -> None:
    payload = {
        "data": {
            "pageNum": 88,
            "pageSize": 10,
            "total": 871,
            "totalPage": 88,
            "list": [{"positionId": "x"}],
        }
    }

    page = XiaohongshuConnector._position_page(payload, expected_page=88)

    assert page["total"] == 871
    assert len(page["rows"]) == 1
