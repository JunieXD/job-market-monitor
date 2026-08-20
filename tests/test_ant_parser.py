import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_market.config import Settings
from job_market.connectors.ant import (
    CATEGORY_API_URL,
    COLLECTION_PAGE_SIZE,
    POSITION_API_URL,
    AntConnector,
)
from job_market.schemas import Channel, JobRecord

FIXTURE = Path(__file__).parent / "fixtures" / "ant_job.json"


def test_ant_job_preserves_direct_source_dimensions() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AntConnector.parse_job(raw)

    assert record.source_key == "ant_cn"
    assert record.external_id == "900001"
    assert record.external_code == "SYNTHETIC-ANT-001"
    assert record.channel is Channel.EXPERIENCED
    assert record.employment_type_id == "social"
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_code == "root/synthetic-unit"
    assert record.department_name == "示例事业部"
    assert record.interview_location_names == ["远程面试"]
    assert record.published_at == datetime(2026, 8, 18, 12, 49, 19, tzinfo=UTC)
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:测试城", "测试城"),
        ("name:第二城市", "第二城市"),
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:技术类-开发", "技术类-开发")
    ]
    assert record.source_payload == raw


def test_ant_rejects_inverted_experience_range() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["experience"] = {"from": 6, "to": 2}

    with pytest.raises(ValueError, match="inverted experience range"):
        AntConnector.parse_job(raw)


def test_ant_validates_page_metadata_and_final_page_size() -> None:
    payload = {
        "content": [{}],
        "totalCount": 50,
        "pageSize": 49,
        "currentPage": 2,
    }

    page = AntConnector._position_page(payload, expected_page=2)

    assert page["total"] == 50
    assert len(page["rows"]) == 1


@pytest.mark.asyncio
async def test_ant_bootstraps_directly_through_public_json_apis() -> None:
    category_payload = {
        "success": True,
        "content": [{"code": "130", "name": "技术类"}],
    }
    position_payload = {
        "success": True,
        "content": [],
        "totalCount": 0,
        "pageSize": COLLECTION_PAGE_SIZE,
        "currentPage": 1,
    }
    evaluate = AsyncMock(
        side_effect=[
            None,
            {"status": 200, "payload": category_payload},
            {"status": 200, "payload": position_payload},
        ]
    )
    context = SimpleNamespace(add_cookies=AsyncMock())
    page = SimpleNamespace(
        context=context,
        goto=AsyncMock(),
        evaluate=evaluate,
    )
    connector = AntConnector(page, Settings(ant_request_delay_seconds=0.5))
    connector._rate_limit = AsyncMock()  # type: ignore[method-assign]

    result = await connector._open_root()

    assert result == position_payload
    context.add_cookies.assert_awaited_once()
    page.goto.assert_awaited_once()
    assert evaluate.await_count == 3
    category_call = evaluate.await_args_list[1]
    position_call = evaluate.await_args_list[2]
    assert category_call.args[1]["url"] == CATEGORY_API_URL
    assert category_call.args[1]["data"] == {}
    assert position_call.args[1]["url"] == POSITION_API_URL
    assert position_call.args[1]["data"]["pageSize"] == COLLECTION_PAGE_SIZE
    assert connector._category_catalog == connector.parse_category_catalog(
        category_payload
    )


@pytest.mark.asyncio
async def test_ant_restarts_collection_when_a_job_changes_between_passes() -> None:
    def payload(rows: list[dict[str, object]], page_number: int) -> dict[str, object]:
        return {
            "content": rows,
            "totalCount": 50,
            "pageSize": COLLECTION_PAGE_SIZE,
            "currentPage": page_number,
        }

    def job(raw: dict[str, object]) -> JobRecord:
        external_id = str(raw["id"])
        version = str(raw["version"])
        return JobRecord(
            source_key="ant_cn",
            external_id=external_id,
            source_url="https://talent.antgroup.com/off-campus",
            company_name="蚂蚁集团",
            channel=Channel.EXPERIENCED,
            employment_type_id="social",
            employment_type_name="社会招聘",
            title=f"岗位 {external_id} {version}",
            source_payload=raw,
        )

    first_page = payload(
        [{"id": index if index < 48 else 0, "version": 1} for index in range(49)],
        1,
    )
    stable_page = payload(
        [
            {"id": index, "version": 2 if index == 0 else 1}
            for index in range(49)
        ],
        1,
    )
    final_page = payload([{"id": 49, "version": 1}], 2)
    connector = AntConnector(SimpleNamespace(), Settings())
    connector.parse_job = job  # type: ignore[method-assign]
    connector._open_root = AsyncMock(side_effect=[stable_page, stable_page])  # type: ignore[method-assign]
    connector._fetch_page = AsyncMock(  # type: ignore[method-assign]
        side_effect=[final_page, final_page, final_page]
    )

    jobs, complete = await connector._collect_root(
        Channel.EXPERIENCED,
        first_page,
        total_count=50,
        max_pages=None,
    )

    assert complete is True
    assert len(jobs) == 50
    assert jobs["0"].title.endswith("2")
    assert connector._open_root.await_count == 2  # type: ignore[attr-defined]
