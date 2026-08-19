import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.oppo import OppoConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "oppo_job.json"
CATEGORIES = {"TEST": "测试类"}
DEGREES = {"UNDERGRADUATE-AND-ABOVE": "本科及以上"}


def test_oppo_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = OppoConnector.parse_job(raw, CATEGORIES, DEGREES)

    assert record.source_key == "oppo_social_cn"
    assert record.external_id == "synthetic-oppo-001"
    assert record.external_code == "J260818000001"
    assert record.channel is Channel.EXPERIENCED
    assert record.description == "负责合成AI产品测试。"
    assert record.requirements == "本科及以上学历，三年以上经验。"
    assert record.published_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.degree_code == "UNDERGRADUATE-AND-ABOVE"
    assert record.degree_name == "本科及以上"
    assert (record.experience_min_years, record.experience_max_years) == (3, 8)
    assert [(item.code, item.name) for item in record.locations] == [
        ("110100", "北京市"),
        ("440300", "深圳市"),
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("TEST", "测试类")
    ]
    assert record.source_payload == raw


def test_oppo_rejects_unaligned_city_codes_and_names() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["workCityName"] = "北京市"

    with pytest.raises(ValueError, match="invalid work-city fields"):
        OppoConnector.parse_job(raw, CATEGORIES, DEGREES)


def test_oppo_validates_dictionary_rows() -> None:
    payload = {
        "data": [
            {
                "dictType": "JOB-TYPE",
                "dictValue": "TEST",
                "dictNameCn": "测试类",
            }
        ]
    }

    assert OppoConnector.parse_dictionary(payload, "JOB-TYPE") == {
        "TEST": "测试类"
    }


def test_oppo_validates_final_page_metadata() -> None:
    payload = {
        "data": {
            "pageNum": 2,
            "pageSize": 10,
            "pages": 2,
            "total": 12,
            "list": [{"positionId": "x"}] * 2,
        }
    }

    page = OppoConnector._position_page(payload, expected_page=2)

    assert page["total"] == 12
    assert len(page["rows"]) == 2
