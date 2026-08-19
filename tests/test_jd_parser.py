import json
from datetime import UTC, datetime
from pathlib import Path

from job_market.connectors.jd import JDConnector, _merge_locations
from job_market.schemas import LocationRecord

FIXTURE = Path(__file__).parent / "fixtures" / "jd_job.json"


def test_jd_job_preserves_category_business_and_location_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = JDConnector.parse_job(raw)

    assert record.source_key == "jd_cn"
    assert record.external_id == "2001"
    assert record.external_code == "JD-FIXTURE-001"
    assert record.title == "示例研发工程师"
    assert record.published_at == datetime.fromtimestamp(1750000000, UTC)
    assert record.is_hot is True
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("YANFA", "研发类")
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("business-example", "示例业务")
    ]
    assert [(item.code, item.name) for item in record.locations] == [
        ("city-a", "测试城")
    ]
    assert record.source_payload == raw


def test_jd_duplicate_requirement_merges_direct_locations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = JDConnector.parse_job(raw)
    second = first.model_copy(
        update={
            "locations": [LocationRecord(code="city-b", name="第二城市")],
            "source_payload": {**raw, "workCityCode": "city-b", "workCity": "第二城市"},
        }
    )

    merged = _merge_locations(first, second)

    assert [item.name for item in merged.locations] == ["测试城", "第二城市"]
