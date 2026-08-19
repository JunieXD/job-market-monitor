import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_market.connectors.meituan import PORTALS, MeituanConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "meituan_job.json"


def test_meituan_job_preserves_direct_source_dimensions() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = MeituanConnector.parse_job(raw, PORTALS[Channel.EXPERIENCED])

    assert record.source_key == "meituan_cn"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "meituan-fixture-001"
    assert record.employment_type_id == "3"
    assert record.employment_type_name == "社会招聘"
    assert record.recruitment_project_id == "project-example"
    assert record.published_at == datetime.fromtimestamp(1750000000, UTC)
    assert record.source_updated_at == datetime.fromtimestamp(1760000000, UTC)
    assert record.department_code == "department-example"
    assert record.department_name == "示例研发部门"
    assert [item.name for item in record.locations] == ["测试城", "第二城市"]
    assert record.categories[0].name == "软件"
    assert record.categories[0].parent_name == "技术类"
    assert record.categories[0].assignment_method.value == "direct_field"
    assert record.source_payload == raw


def test_meituan_rejects_inconsistent_pagination_metadata() -> None:
    payload = {
        "data": {
            "list": [{}],
            "page": {"pageNo": 1, "pageSize": 10, "totalPage": 2, "totalCount": 1},
        }
    }

    with pytest.raises(RuntimeError, match="inconsistent"):
        MeituanConnector._position_page(payload, expected_page=1)
