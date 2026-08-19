import json
from datetime import UTC, datetime
from pathlib import Path

from job_market.connectors.bytedance import ByteDanceConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "bytedance_job.json"


def test_parse_preserves_source_facts_and_multi_city_locations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = ByteDanceConnector.parse_job(raw, Channel.EXPERIENCED)

    assert record.external_id == "fixture-001"
    assert record.external_code == "A-FIXTURE"
    assert record.channel is Channel.EXPERIENCED
    assert len(record.categories) == 1
    assert record.categories[0].external_id == "cat-backend"
    assert record.categories[0].name == "后端"
    assert record.categories[0].parent_external_id == "cat-rd"
    assert record.categories[0].parent_name == "研发"
    assert record.categories[0].assignment_method.value == "direct_field"
    assert [location.name for location in record.locations] == ["测试城", "第二城市"]
    assert record.locations[0].district_name == "测试区"
    assert record.published_at == datetime.fromtimestamp(1760000000, UTC)
    assert record.source_payload == raw
    assert len(record.content_hash()) == 64


def test_parse_hot_flag_does_not_treat_string_zero_as_true() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["job_hot_flag"] = "0"

    record = ByteDanceConnector.parse_job(raw, Channel.EXPERIENCED)

    assert record.is_hot is False
