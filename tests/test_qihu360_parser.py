import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.qihu360 import (
    LIST_LIMIT,
    POSITION_DETAIL_API_URL,
    Qihu360Connector,
)
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "qihu360_job.json"


def test_qihu360_job_preserves_detail_facts_and_list_date() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = Qihu360Connector.parse_job(raw["list"], raw["detail"])

    assert record.source_key == "qihu360_cn"
    assert record.external_id == "synthetic-360-001"
    assert record.channel is Channel.EXPERIENCED
    assert record.description == "岗位职责\n负责合成AI产品设计。"
    assert record.requirements == "具备三年以上产品经验。"
    assert record.published_at == datetime(
        2026,
        8,
        1,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert (record.experience_min_years, record.experience_max_years) == (3, 8)
    assert [(item.code, item.name) for item in record.locations] == [
        ("name:测试城", "测试城")
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:产品", "产品")
    ]
    assert record.source_payload == raw


def test_qihu360_keeps_missing_requirements_null() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["detail"]["description"] = "工作职责：\n只提供职责的合成岗位。"
    raw["detail"]["year"] = "不限"

    record = Qihu360Connector.parse_job(raw["list"], raw["detail"])

    assert record.description == "工作职责：\n只提供职责的合成岗位。"
    assert record.requirements is None
    assert record.experience_min_years is None
    assert record.experience_max_years is None


def test_qihu360_rejects_list_detail_drift() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["detail"]["title"] = "另一岗位"

    with pytest.raises(ValueError, match="changed field 'title'"):
        Qihu360Connector.parse_job(raw["list"], raw["detail"])


def test_qihu360_list_validates_declared_count_and_cap() -> None:
    rows, total = Qihu360Connector._list_rows(
        {"code": 0, "count": 2, "data": [{"id": "1"}, {"id": "2"}]}
    )
    assert total == 2
    assert len(rows) == 2

    with pytest.raises(RuntimeError, match="request cap"):
        Qihu360Connector._list_rows(
            {"code": 0, "count": LIST_LIMIT, "data": [{}] * LIST_LIMIT}
        )


@pytest.mark.asyncio
async def test_qihu360_detail_uses_same_origin_api_without_page_navigation() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(return_value={"code": 0, "data": raw["detail"]}),
    )
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    connector = object.__new__(Qihu360Connector)
    connector.settings = SimpleNamespace(qihu360_request_delay_seconds=0.5)
    connector._last_request_started_at = 0.0
    connector._request_start_lock = asyncio.Lock()

    result = await connector._open_detail(
        SimpleNamespace(request=request),
        asyncio.Queue(),
        raw["list"]["id"],
    )

    assert result["data"] == raw["detail"]
    request.get.assert_awaited_once_with(
        POSITION_DETAIL_API_URL.format(external_id=raw["list"]["id"]),
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": (
                "https://hr.360.cn/hr/detail/"
                f"{raw['list']['id']}"
            ),
        },
        timeout=60_000,
    )
