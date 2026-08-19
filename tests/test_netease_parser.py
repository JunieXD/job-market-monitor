import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
    payload = {"data": {"list": [], "pages": 2, "total": 201}}

    with pytest.raises(RuntimeError, match="row mismatch"):
        NetEaseConnector._page_data(payload, 1)


def test_netease_validates_large_final_page_shape() -> None:
    payload = {
        "data": {
            "list": [{"id": str(index)} for index in range(157)],
            "pages": 13,
            "total": 2557,
        }
    }

    page = NetEaseConnector._page_data(payload, 13)

    assert page["total"] == 2557
    assert len(page["rows"]) == 157


def test_netease_rejects_pagination_metadata_mismatch() -> None:
    payload = {"data": {"list": [{}] * 200, "pages": 14, "total": 2557}}

    with pytest.raises(RuntimeError, match="metadata mismatch"):
        NetEaseConnector._page_data(payload, 1)


def test_netease_rejects_invalid_recruitment_count() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["recruitNum"] = -1

    with pytest.raises(ValueError, match="recruitNum"):
        NetEaseConnector.parse_job(raw)


def test_netease_preserves_job_without_optional_text_or_location() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.pop("description")
    raw.pop("requirement")
    raw.pop("workPlaceNameList")

    record = NetEaseConnector.parse_job(raw)

    assert record.description is None
    assert record.requirements is None
    assert record.locations == []


async def test_netease_dynamic_list_returns_partial_observations() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = {"data": {"list": [raw, deepcopy(raw)], "pages": 1, "total": 2}}
    connector = NetEaseConnector(
        None,
        SimpleNamespace(netease_request_delay_seconds=0.5),
    )

    async def same_page(page_number):
        return payload

    connector._fetch_page = same_page
    jobs, _, complete, _ = await connector._collect_root(
        Channel.GENERAL,
        payload,
        2,
        None,
    )

    assert complete is False
    assert len(jobs) == 1
    assert connector.issues[-1].error_type == "DynamicListDidNotConverge"
