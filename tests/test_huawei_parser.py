import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.huawei import (
    PORTALS,
    HuaweiConnector,
    _missing_required_source_fields,
)
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "huawei_job.json"


def test_huawei_job_preserves_direct_category_business_and_locations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = HuaweiConnector.parse_job(raw, PORTALS[Channel.EXPERIENCED])

    assert record.source_key == "huawei_cn"
    assert record.external_id == "900001"
    assert record.external_code == "800001"
    assert record.employment_type_id == "1"
    assert record.experience_min_years == 3
    assert record.degree_name == "本科"
    assert record.source_status == "online"
    assert record.published_at == datetime(
        2026,
        4,
        13,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.source_updated_at == datetime(
        2026,
        4,
        16,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("category:研发族", "研发族")
    ]
    assert [item.name for item in record.business_units] == [
        "示例业务部",
        "示例研究所",
        "示例集团",
    ]
    assert [item.name for item in record.locations] == ["测试城", "第二城市"]
    assert record.source_payload == raw


def test_huawei_campus_uses_official_recruitment_scenario() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.update(
        {
            "jobType": "0",
            "recruitScenarioId": "fixture-campus",
            "scenarioName": "应届生",
            "category": "fixture-category",
            "categoryName": "研发类",
        }
    )

    record = HuaweiConnector.parse_job(raw, PORTALS[Channel.CAMPUS])

    assert record.channel is Channel.CAMPUS
    assert record.employment_type_id == "fixture-campus"
    assert record.employment_type_name == "应届生"
    assert record.recruitment_project_id == "fixture-campus"
    assert record.categories[0].external_id == "fixture-category"


def test_huawei_rejects_inconsistent_pagination_metadata() -> None:
    payload = {
        "data": {
            "pageVO": {
                "curPage": 1,
                "pageSize": 10,
                "totalRows": 1,
                "totalPages": 2,
            },
            "result": [{}],
        }
    }

    with pytest.raises(RuntimeError, match="inconsistent"):
        HuaweiConnector._page_data(payload, expected_page=1)


def test_huawei_identifies_incomplete_source_record_without_inventing_fields() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["jobName"] = None
    raw["jobNameNew"] = None
    raw["jobRequire"] = None

    assert _missing_required_source_fields(raw) == (
        "jobName/jobNameNew",
        "jobRequire",
    )
