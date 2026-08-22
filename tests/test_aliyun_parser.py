import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from job_market.connectors.aliyun import AliyunConnector
from job_market.schemas import CategoryAssignmentMethod, Channel

FIXTURE = Path(__file__).parent / "fixtures" / "aliyun_job.json"


def test_aliyun_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AliyunConnector.parse_job(raw)

    assert record.source_key == "alibaba_cloud_social"
    assert record.company_name == "阿里云"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "synthetic-aliyun-001"
    assert record.external_code == "GP-SYNTHETIC-001"
    assert str(record.source_url) == (
        "https://careers.aliyun.com/off-campus/position-detail"
        "?positionId=synthetic-aliyun-001"
    )
    assert record.description == "负责合成云平台研发。"
    assert record.requirements == "具备扎实的软件工程能力。"
    assert record.published_at == datetime.fromtimestamp(1786982400, UTC)
    assert record.source_updated_at == datetime.fromtimestamp(1786986000, UTC)
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_name == "云智能集团"
    assert [(item.code, item.name) for item in record.locations] == [
        ("city:杭州", "杭州"),
        ("city:上海", "上海"),
    ]
    categories = [
        (item.external_id, item.name, item.assignment_method)
        for item in record.categories
    ]
    assert categories == [
        ("category:130", "技术类", CategoryAssignmentMethod.DIRECT_FIELD)
    ]
    assert record.source_payload == raw


def test_aliyun_accepts_known_broken_pagination_metadata() -> None:
    payload = {
        "content": {
            "datas": [{"id": str(index)} for index in range(8)],
            "totalCount": 668,
            "pageSize": 500,
            "currentPage": 1,
        }
    }

    page = AliyunConnector._position_page(payload, expected_page=67)

    assert page["total"] == 668
    assert len(page["rows"]) == 8


class _RootPassConnector(AliyunConnector):
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.pages_fetched = 0
        self.snapshots = []

    async def _open_root(self) -> dict[str, Any]:
        return self.pages[0]

    async def _next_page(self, page_number: int) -> dict[str, Any]:
        return self.pages[page_number - 1]

    def _save_payload(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_aliyun_accepts_stable_unique_count_when_declared_total_is_stale() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = []
    for index in range(500):
        row = dict(raw)
        row["id"] = f"synthetic-{index:03d}"
        row["code"] = f"CODE-{index:03d}"
        row["name"] = f"Synthetic role {index}"
        rows.append(row)

    pages = []
    for offset in range(0, len(rows), 10):
        page_rows = rows[offset : offset + 10]
        pages.append(
            {
                "success": True,
                "content": {
                    "currentPage": len(pages) + 1,
                    "pageSize": 10,
                    "totalCount": 691,
                    "datas": page_rows,
                },
            }
        )

    connector = _RootPassConnector(pages)
    records, declared, initial, passes, complete = await connector._collect_root_records(
        Channel.EXPERIENCED,
        None,
    )

    assert complete is True
    assert declared == initial == 691
    assert len(records) == 500
    assert passes == 2
