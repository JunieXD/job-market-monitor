import json
from pathlib import Path

import pytest

from job_market.connectors.tencent import TencentConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "tencent_job.json"


def test_tencent_detail_preserves_project_business_groups_and_interview_cities() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = TencentConnector.parse_job(raw)

    assert record.source_key == "tencent_cn"
    assert record.channel is Channel.CAMPUS
    assert record.external_id == "tencent-fixture-001"
    assert record.external_code == "6001"
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "应届毕业生"
    assert record.recruitment_project_id == "8001"
    assert record.recruitment_project_name == "示例校园招聘"
    assert record.interview_location_names == ["远程面试", "测试城"]
    assert [(item.code, item.name) for item in record.locations] == [
        ("1", "测试城"),
        ("3", "第二城市"),
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("7001", "示例事业群")
    ]
    assert record.categories[0].external_id == "2"
    assert record.categories[0].name == "技术"
    assert record.source_payload == raw


def test_tencent_rejects_more_rows_than_the_official_page_size() -> None:
    payload = {"data": {"positionList": [{}] * 11, "count": 11}}

    with pytest.raises(RuntimeError, match="exceeds UI page size"):
        TencentConnector._search_page(payload, expected_page=1)
