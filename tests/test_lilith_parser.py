import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.lilith import API_CAP, PAGE_SIZE, PORTALS, LilithConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "lilith_job.json"


def test_lilith_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = LilithConnector.parse_job(raw, PORTALS[Channel.CAMPUS])

    assert record.source_key == "lilith_cn"
    assert record.external_id == "synthetic-lilith-001"
    assert record.external_code == "LILITH-SYNTHETIC-001"
    assert record.channel is Channel.CAMPUS
    assert record.employment_type_id == "201"
    assert record.employment_type_name == "正式"
    assert record.recruitment_project_id == "synthetic-project"
    assert record.recruitment_project_name == "合成校园项目"
    assert record.published_at == datetime(
        2026,
        8,
        17,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.department_code == "synthetic-department"
    assert record.source_status == "active"
    assert record.is_hot is True
    assert [(item.code, item.name, item.address) for item in record.locations] == [
        ("CT_SYNTHETIC_SH", "上海", "上海市·闵行区")
    ]
    category = record.categories[0]
    assert (category.external_id, category.name) == (
        "synthetic-function-child",
        "研发",
    )
    assert (category.parent_external_id, category.parent_name) == (
        "synthetic-function-root",
        "技术",
    )
    assert record.source_payload == raw


def test_lilith_rejects_recruit_type_from_another_channel() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="does not match channel experienced"):
        LilithConnector.parse_job(raw, PORTALS[Channel.EXPERIENCED])


def test_lilith_rejects_api_cap_and_bad_final_page() -> None:
    with pytest.raises(RuntimeError, match=str(API_CAP)):
        LilithConnector._position_page(
            {"data": {"job_post_list": [], "count": API_CAP}},
            expected_page=1,
        )

    with pytest.raises(RuntimeError, match="row mismatch"):
        LilithConnector._position_page(
            {
                "data": {
                    "job_post_list": [{"id": "only-one"}],
                    "count": PAGE_SIZE + 2,
                }
            },
            expected_page=2,
        )


async def test_lilith_campus_excludes_official_internship_overlap() -> None:
    page = SimpleNamespace(on=Mock())
    connector = LilithConnector(
        page,
        SimpleNamespace(lilith_request_delay_seconds=0.5),
    )
    campus = json.loads(FIXTURE.read_text(encoding="utf-8"))
    unique = {**campus, "id": "campus-only"}
    overlap = {**campus, "id": "shared-job"}
    connector._collect_stable = AsyncMock(
        side_effect=[
            (
                {"campus-only": unique, "shared-job": overlap},
                2,
                True,
                [],
            ),
            ({"shared-job": overlap}, 1, True, []),
        ]
    )

    result = await connector.collect(Channel.CAMPUS)

    assert result.complete is True
    assert [job.external_id for job in result.jobs] == ["campus-only"]
    assert result.partition_counts["excluded-intern-overlap"] == 1


async def test_lilith_rejects_duplicate_ids_across_pages() -> None:
    page = SimpleNamespace(on=Mock())
    connector = LilithConnector(
        page,
        SimpleNamespace(lilith_request_delay_seconds=0.5),
    )
    first_rows = [{"id": f"job-{index}"} for index in range(PAGE_SIZE)]
    connector._open_page = AsyncMock(
        side_effect=[
            {"data": {"job_post_list": first_rows, "count": PAGE_SIZE + 1}},
            {"data": {"job_post_list": [{"id": "job-0"}], "count": PAGE_SIZE + 1}},
        ]
    )

    with pytest.raises(RuntimeError, match="repeated job job-0"):
        await connector._collect_pass(
            PORTALS[Channel.CAMPUS],
            partition="test",
            max_pages=None,
        )
