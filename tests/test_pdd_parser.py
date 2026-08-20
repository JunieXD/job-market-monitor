import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.pdd import DETAIL_ENDPOINT, PORTALS, PDDConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "pdd_job.json"


def test_pdd_job_preserves_list_and_detail_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = PDDConnector.parse_job(
        raw["list"],
        raw["detail"],
        PORTALS[Channel.CAMPUS],
    )

    assert record.source_key == "pdd_cn"
    assert record.external_id == "synthetic-pdd-001"
    assert record.external_code == "SYNTHETIC001"
    assert record.channel is Channel.CAMPUS
    assert record.employment_type_id == "grad"
    assert record.employment_type_name == "应届生招聘"
    assert record.recruitment_project_id is None
    assert record.recruitment_project_name == "技术专场"
    assert record.published_at == datetime(
        2026,
        7,
        5,
        21,
        9,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.source_status == "inTime"
    assert [(item.code, item.name) for item in record.locations] == [
        ("synthetic-city", "测试城")
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("technology", "技术")
    ]
    assert record.is_hot is None
    assert record.graduation_start_at is None
    assert record.graduation_end_at is None
    assert record.source_payload == raw


def test_pdd_rejects_list_detail_drift() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["detail"]["jobName"] = "另一分类"

    with pytest.raises(ValueError, match="changed field 'jobName'"):
        PDDConnector.parse_job(
            raw["list"],
            raw["detail"],
            PORTALS[Channel.CAMPUS],
        )


def test_pdd_list_page_accepts_string_total_and_validates_final_page() -> None:
    payload = {"result": {"list": [{}, {}], "total": "22"}}

    page = PDDConnector._list_page(payload, expected_page=3)

    assert page["total"] == 22
    assert len(page["rows"]) == 2


def test_pdd_list_page_rejects_duplicate_count_shape() -> None:
    payload = {"result": {"list": [{}] * 10, "total": "2"}}

    with pytest.raises(RuntimeError, match="row mismatch"):
        PDDConnector._list_page(payload, expected_page=1)


@pytest.mark.asyncio
async def test_pdd_detail_uses_same_origin_api_without_page_navigation() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(return_value={"success": True, "result": raw["detail"]}),
    )
    request = SimpleNamespace(post=AsyncMock(return_value=response))
    connector = object.__new__(PDDConnector)
    connector.settings = SimpleNamespace(pdd_request_delay_seconds=0.5)
    connector._last_request_started_at = 0.0
    connector._request_start_lock = asyncio.Lock()

    result = await connector._open_detail(
        SimpleNamespace(request=request),
        asyncio.Queue(),
        PORTALS[Channel.CAMPUS],
        raw["list"]["id"],
    )

    assert result["result"] == raw["detail"]
    request.post.assert_awaited_once_with(
        DETAIL_ENDPOINT,
        data={"id": raw["list"]["id"], "t": None},
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PORTALS[Channel.CAMPUS].detail_url(raw["list"]["id"]),
        },
        timeout=60_000,
    )
