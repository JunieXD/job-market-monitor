import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.ctrip import CtripConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "ctrip_job.json"


def test_ctrip_job_splits_explicit_source_sections() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = CtripConnector.parse_job(raw)

    assert record.source_key == "ctrip_cn"
    assert record.external_id == "29524093"
    assert record.external_code == "MJ-SYNTHETIC-001"
    assert record.channel is Channel.GENERAL
    assert record.employment_type_id == "experienced-page-mixed"
    assert record.description == "负责示例Agent平台的研发。"
    assert record.requirements == "本科及以上学历，具备工程实践能力。"
    assert record.published_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [(item.code, item.name) for item in record.locations] == [
        ("CO0009", "测试城")
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("JFG_33", "AI & BI")
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("47", "住宿业务")
    ]
    assert str(record.source_url).endswith("/experienced/job-detail/MJ-SYNTHETIC-001")
    assert record.source_payload == raw


def test_ctrip_uses_explicit_duty_when_supplied() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["duty"] = "负责官网明确提供的职责。"
    raw["requirements"] = "本科及以上学历。"

    record = CtripConnector.parse_job(raw)

    assert record.description == "负责官网明确提供的职责。"
    assert record.requirements == "本科及以上学历。"


def test_ctrip_keeps_incomplete_business_unit_only_in_raw_payload() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["buName"] = None

    record = CtripConnector.parse_job(raw)

    assert record.business_units == []
    assert record.source_payload["buCode"] == "47"
    assert record.source_payload["buName"] is None


def test_ctrip_keeps_missing_structured_location_only_in_raw_payload() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["city"] = None
    raw["cityName"] = None
    raw["jobTitle"] = "标题中的城市不能当作结构化地点"

    record = CtripConnector.parse_job(raw)

    assert record.locations == []
    assert record.source_payload["city"] is None
    assert record.source_payload["cityName"] is None


def test_ctrip_rejects_incomplete_structured_location_pair() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["city"] = None

    with pytest.raises(ValueError, match="incomplete structured city"):
        CtripConnector.parse_job(raw)


def test_ctrip_preserves_unstructured_combined_body_without_guessing() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["requirements"] = "只有一段没有标题的正文。"

    record = CtripConnector.parse_job(raw)

    assert record.description == "只有一段没有标题的正文。"
    assert record.requirements is None


def test_ctrip_splits_explicit_english_requirements_heading() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["requirements"] = """
        <p>Job Overview</p>
        <p>Build and operate a synthetic partner platform.</p>
        <p>Key Responsibilities</p>
        <p>Manage the complete synthetic partner lifecycle.</p>
        <p>Requirements</p>
        <p>Bachelor's degree or above.</p>
    """

    record = CtripConnector.parse_job(raw)

    assert "Job Overview" in record.description
    assert "Key Responsibilities" in record.description
    assert record.requirements == "Bachelor's degree or above."


def test_ctrip_keeps_one_sided_explicit_sections_null() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["requirements"] = "任职要求\n只提供要求。"

    record = CtripConnector.parse_job(raw)

    assert record.description is None
    assert record.requirements == "只提供要求。"


def test_ctrip_validates_final_page_size() -> None:
    payload = {
        "retValue": {
            "recruitJobAdList": [{"id": "x"}] * 2,
            "total": 22,
        }
    }

    page = CtripConnector._position_page(
        payload,
        expected_page=2,
        page_size=20,
    )

    assert page["total"] == 22
    assert len(page["rows"]) == 2
