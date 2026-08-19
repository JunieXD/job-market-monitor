import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_market.connectors.tongcheng import TongchengConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "tongcheng_job.json"


def test_tongcheng_job_preserves_direct_list_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = TongchengConnector.parse_job(raw)

    assert record.source_key == "tongcheng_social_cn"
    assert record.company_name == "同程旅行"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "synthetic-tongcheng-001"
    assert record.employment_type_id == "正式-正式"
    assert record.employment_type_name == "正式-正式"
    assert record.description is None
    assert record.requirements is None
    assert record.published_at == datetime(
        2026, 8, 18, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:北京", "北京"),
        ("name:苏州", "苏州"),
    ]
    assert record.categories == []
    assert record.source_payload == raw


def test_tongcheng_validates_terminal_page_shape() -> None:
    payload = {
        "code": 0,
        "data": {
            "totalElements": 501,
            "totalPages": 51,
            "content": [{"id": "x"}],
        },
    }

    page = TongchengConnector._position_page(payload, expected_page=51)

    assert page == {"rows": [{"id": "x"}], "total": 501, "pages": 51}
