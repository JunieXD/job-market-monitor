import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_market.connectors.ant import AntConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "ant_job.json"


def test_ant_job_preserves_direct_source_dimensions() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AntConnector.parse_job(raw)

    assert record.source_key == "ant_cn"
    assert record.external_id == "900001"
    assert record.external_code == "SYNTHETIC-ANT-001"
    assert record.channel is Channel.EXPERIENCED
    assert record.employment_type_id == "social"
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_code == "root/synthetic-unit"
    assert record.department_name == "示例事业部"
    assert record.interview_location_names == ["远程面试"]
    assert record.published_at == datetime(2026, 8, 18, 12, 49, 19, tzinfo=UTC)
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:测试城", "测试城"),
        ("name:第二城市", "第二城市"),
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:技术类-开发", "技术类-开发")
    ]
    assert record.source_payload == raw


def test_ant_rejects_inverted_experience_range() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["experience"] = {"from": 6, "to": 2}

    with pytest.raises(ValueError, match="inverted experience range"):
        AntConnector.parse_job(raw)


def test_ant_validates_page_metadata_and_final_page_size() -> None:
    payload = {
        "content": [{}],
        "totalCount": 11,
        "pageSize": 10,
        "currentPage": 2,
    }

    page = AntConnector._position_page(payload, expected_page=2)

    assert page["total"] == 11
    assert len(page["rows"]) == 1
