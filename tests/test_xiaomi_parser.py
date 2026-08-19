import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.xiaomi import PORTALS, XiaomiConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "xiaomi_job.json"


def test_xiaomi_job_uses_explicit_project_type_and_source_url() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = XiaomiConnector.parse_job(raw, PORTALS[Channel.EXPERIENCED])

    assert record.source_key == "xiaomi_cn"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "xiaomi-post-fixture"
    assert record.external_code == "MI-FIXTURE-001"
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "社招"
    assert record.published_at == datetime(2026, 4, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert record.department_name == "示例部门"
    assert record.categories == []
    assert [item.name for item in record.locations] == ["测试城", "第二城市"]
    assert str(record.source_url).endswith("/index/position/fixture/detail")


def test_xiaomi_rejects_response_type_from_another_project() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="expected 2"):
        XiaomiConnector.parse_job(raw, PORTALS[Channel.CAMPUS])


def test_xiaomi_validates_known_job_types_before_channel_filtering() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["type"] = 99

    with pytest.raises(ValueError, match="unknown type"):
        XiaomiConnector._job_identity(raw)


def test_xiaomi_rejects_inconsistent_pagination_metadata() -> None:
    payload = {
        "data": {"list": [{}], "pageNum": 1, "pageSize": 10, "pageTotal": 2, "total": 1}
    }

    with pytest.raises(RuntimeError, match="inconsistent"):
        XiaomiConnector._page_data(payload, expected_page=1)
