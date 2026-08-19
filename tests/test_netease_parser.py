import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_market.connectors.netease import NetEaseConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "netease_job.json"


def test_netease_job_keeps_mixed_work_type_and_direct_requirements() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = NetEaseConnector.parse_job(raw)

    assert record.source_key == "netease_cn"
    assert record.channel is Channel.GENERAL
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "实习"
    assert record.degree_name == "本科"
    assert (record.experience_min_years, record.experience_max_years) == (0, 3)
    assert record.department_name == "示例事业部"
    assert record.recruitment_count == 2
    assert record.source_updated_at == datetime.fromtimestamp(1760000000, UTC)
    assert [(item.code, item.name) for item in record.business_units] == [
        ("P-EXAMPLE", "示例产品")
    ]
    assert [item.name for item in record.locations] == ["测试城", "第二城市"]
    assert record.categories[0].name == "产品"
    assert record.source_payload == raw


def test_netease_rejects_empty_non_terminal_page() -> None:
    payload = {"data": {"list": [], "pages": 2, "total": 11}}

    with pytest.raises(RuntimeError, match="empty non-terminal"):
        NetEaseConnector._page_data(payload, 1)


def test_netease_rejects_invalid_recruitment_count() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["recruitNum"] = -1

    with pytest.raises(ValueError, match="recruitNum"):
        NetEaseConnector.parse_job(raw)
