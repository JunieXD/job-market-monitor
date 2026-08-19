import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_market.connectors.beike import BeikeConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "beike_job.json"


def test_beike_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = BeikeConnector.parse_job(raw)

    assert record.source_key == "beike_social_cn"
    assert record.company_name == "贝壳"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "synthetic-beike-001"
    assert record.external_code == "100001"
    assert record.description == "负责合成数据平台建设。"
    assert record.requirements == "具备工程实践能力。"
    assert record.published_at == datetime.fromtimestamp(1786982400, UTC)
    assert record.source_updated_at == datetime(
        2026, 8, 18, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert record.degree_name == "本科及以上"
    assert (record.experience_min_years, record.experience_max_years) == (3, 3)
    assert record.department_code == "200001"
    assert record.department_name == "数据治理单元"
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:北京市", "北京市")
    ]
    assert record.categories == []
    assert record.source_payload == raw


def test_beike_validates_terminal_page_shape() -> None:
    payload = {"Code": 200, "Count": 394, "Data": [{"Id": "x"}] * 14}

    page = BeikeConnector._position_page(payload, expected_page_index=19)

    assert page["total"] == 394
    assert len(page["rows"]) == 14
