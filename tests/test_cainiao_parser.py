import json
from datetime import UTC, datetime
from pathlib import Path

from job_market.connectors.cainiao import CainiaoConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "cainiao_job.json"


def test_cainiao_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = CainiaoConnector.parse_job(raw)

    assert record.source_key == "alibaba_cainiao_social"
    assert record.company_name == "菜鸟集团"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "100009990001"
    assert record.external_code == "GP100009990001"
    assert str(record.source_url) == (
        "https://talent.cainiao.com/off-campus/position-detail"
        "?positionId=100009990001&positionType=social"
    )
    assert record.description == "负责合成智能体平台研发。"
    assert record.requirements == "具备扎实的软件工程能力。"
    assert record.published_at == datetime.fromtimestamp(1786982400, UTC)
    assert record.source_updated_at == datetime.fromtimestamp(1786986000, UTC)
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_name == "菜鸟"
    assert [(item.code, item.name) for item in record.locations] == [
        ("city:杭州", "杭州"),
        ("city:上海", "上海"),
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:技术类-开发", "技术类-开发")
    ]
    assert record.source_payload == raw


def test_cainiao_allows_source_optional_fields_to_be_null() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["description"] = None
    raw["requirement"] = None
    raw["workLocations"] = None
    raw["categories"] = None

    record = CainiaoConnector.parse_job(raw)

    assert record.description is None
    assert record.requirements is None
    assert record.locations == []
    assert record.categories == []


def test_cainiao_validates_final_page_shape() -> None:
    payload = {
        "content": {
            "datas": [{"id": "x"}] * 4,
            "totalCount": 84,
            "pageSize": 10,
            "currentPage": 9,
        }
    }

    page = CainiaoConnector._position_page(payload, expected_page=9)

    assert page["total"] == 84
    assert len(page["rows"]) == 4
