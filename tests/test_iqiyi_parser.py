import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.iqiyi import IqiyiConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "iqiyi_job.json"


def test_iqiyi_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = IqiyiConnector.parse_job(raw, Channel.CAMPUS)

    assert record.source_key == "iqiyi_cn"
    assert record.external_id == "synthetic-iqiyi-001"
    assert record.external_code == "A999001"
    assert record.channel is Channel.CAMPUS
    assert record.employment_type_id == "202"
    assert record.employment_type_name == "实习"
    assert record.recruitment_project_id == "project-1"
    assert record.recruitment_project_name == "日常实习生招聘"
    assert record.description == "负责合成智能体平台研发。"
    assert record.requirements == "具备扎实的软件工程能力。"
    assert record.is_hot is True
    assert record.published_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [(item.code, item.name) for item in record.locations] == [
        ("CT_11", "北京"),
        ("CT_125", "上海"),
    ]
    category = record.categories[0]
    assert (category.external_id, category.name) == ("function-child", "研发")
    assert (category.parent_external_id, category.parent_name) == (
        "function-root",
        "技术",
    )
    assert record.source_payload == raw


def test_iqiyi_rejects_recruit_type_from_another_channel() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="does not match channel experienced"):
        IqiyiConnector.parse_job(raw, Channel.EXPERIENCED)


def test_iqiyi_allows_missing_optional_dimensions() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["city_list"] = None
    raw["job_function"] = None
    raw["job_subject"] = None

    record = IqiyiConnector.parse_job(raw, Channel.CAMPUS)

    assert record.locations == []
    assert record.categories == []
    assert record.recruitment_project_id is None


def test_iqiyi_validates_final_page_shape() -> None:
    payload = {
        "data": {
            "job_post_list": [{"id": "x"}] * 3,
            "count": 63,
            "extra": "",
        }
    }

    page = IqiyiConnector._position_page(payload, expected_page=7)

    assert page["total"] == 63
    assert len(page["rows"]) == 3
