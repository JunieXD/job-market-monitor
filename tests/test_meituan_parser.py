import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
            "page": {"pageNo": 1, "pageSize": 200, "totalPage": 2, "totalCount": 1},
        }
    }

    with pytest.raises(RuntimeError, match="inconsistent"):
        MeituanConnector._position_page(payload, expected_page=1)


def test_meituan_validates_large_final_page_shape() -> None:
    payload = {
        "data": {
            "list": [{}] * 157,
            "page": {
                "pageNo": 3,
                "pageSize": 200,
                "totalPage": 3,
                "totalCount": 557,
            },
        }
    }

    page = MeituanConnector._position_page(payload, expected_page=3)

    assert page["total_count"] == 557
    assert len(page["rows"]) == 157


def test_meituan_rejects_unexpected_page_size() -> None:
    payload = {
        "data": {
            "list": [{}] * 10,
            "page": {"pageNo": 1, "pageSize": 10, "totalPage": 1, "totalCount": 10},
        }
    }

    with pytest.raises(RuntimeError, match="page size changed"):
        MeituanConnector._position_page(payload, expected_page=1)


def test_meituan_preserves_job_without_optional_text_or_location() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.pop("jobDuty")
    raw.pop("jobRequirement")
    raw.pop("cityList")

    record = MeituanConnector.parse_job(raw, PORTALS[Channel.EXPERIENCED])

    assert record.description is None
    assert record.requirements is None
    assert record.locations == []


async def test_meituan_dynamic_list_returns_partial_observations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = {
        "data": {
            "list": [raw, deepcopy(raw)],
            "page": {"pageNo": 1, "pageSize": 200, "totalPage": 1, "totalCount": 2},
        }
    }
    connector = MeituanConnector(
        None,
        SimpleNamespace(meituan_request_delay_seconds=0.5),
    )

    async def same_page(portal, page_number):
        return payload

    connector._fetch_page = same_page
    jobs, _, complete, _ = await connector._collect_root(
        PORTALS[Channel.EXPERIENCED],
        payload,
        2,
        None,
    )

    assert complete is False
    assert len(jobs) == 1
    assert connector.issues[-1].error_type == "DynamicListDidNotConverge"
