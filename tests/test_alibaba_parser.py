import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from job_market.connectors.alibaba import (
    BATCH_OPEN_ATTEMPTS,
    AlibabaConnector,
    AlibabaSourceCatalog,
    RecruitmentBatch,
)
from job_market.connectors.browser_json import BrowserResponseUnavailableError
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "alibaba_job.json"


def source_catalog() -> AlibabaSourceCatalog:
    return AlibabaSourceCatalog(
        category_codes_by_name={"技术类": "11"},
        business_units_by_code={
            "example-group": "示例业务集团",
            "second-group": "第二示例集团",
        },
    )


def test_alibaba_job_parses_only_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AlibabaConnector.parse_job(raw, source_catalog())

    assert record.source_key == "alibaba_cn"
    assert record.company_name == "阿里巴巴集团"
    assert record.channel is Channel.CAMPUS
    assert record.external_id == "ali-fixture-001"
    assert record.external_code is None
    assert str(record.source_url).startswith(
        "https://campus-talent.alibaba.com/campus/position/ali-fixture-001"
    )
    assert record.published_at is None
    assert record.source_updated_at == datetime.fromtimestamp(1760000000, UTC)
    assert record.source_status == "recruit"
    assert record.employment_type_id == "freshman"
    assert record.employment_type_name == "应届生"
    assert record.recruitment_project_id == "100000000001"
    assert record.recruitment_project_name == "示例届应届生项目"
    assignments = [
        (item.external_id, item.name, item.assignment_method.value)
        for item in record.categories
    ]
    assert assignments == [
        ("11", "技术类", "direct_field"),
    ]
    assert record.graduation_start_at == datetime.fromtimestamp(1750000000, UTC)
    assert record.graduation_end_at == datetime.fromtimestamp(1760000000, UTC)
    assert record.interview_location_names == ["远程", "测试城"]
    assert [(item.code, item.name) for item in record.locations] == [
        ("city:测试城", "测试城"),
        ("city:第二城市", "第二城市"),
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("example-group", "示例业务集团"),
    ]
    assert record.source_payload == raw
    assert record.source_payload["circleNames"] == ["示例业务集团"]


def test_alibaba_batch_discovery_deduplicates_reused_batch_ids() -> None:
    batches = AlibabaConnector._parse_batches(
        {
            "content": {
                "graduate": [{"id": 101, "name": "应届生"}],
                "internship": [{"id": 202, "name": "日常实习"}],
                "topTalentPlan": [{"id": 101, "name": "重复入口"}],
            }
        }
    )

    assert [(item.id, item.name, item.kind) for item in batches] == [
        (101, "应届生", "graduate"),
        (202, "日常实习", "internship"),
    ]


def test_alibaba_business_units_are_resolved_by_code_not_array_index() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["circleCodeList"] = ["second-group", "example-group"]
    raw["circleNames"] = ["示例业务集团", "第二示例集团"]

    record = AlibabaConnector.parse_job(raw, source_catalog())

    assert [(item.code, item.name) for item in record.business_units] == [
        ("second-group", "第二示例集团"),
        ("example-group", "示例业务集团"),
    ]


def test_alibaba_business_units_without_codes_are_rejected() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.pop("circleCodeList")

    with pytest.raises(ValueError, match="circleNames"):
        AlibabaConnector.parse_job(raw, source_catalog())


def test_alibaba_condition_catalog_preserves_official_codes() -> None:
    catalog = AlibabaConnector._parse_source_catalog(
        {
            "content": {
                "searchItems": [
                    {
                        "type": "category",
                        "items": [{"value": "11", "label": "技术类"}],
                    },
                    {
                        "type": "customDept",
                        "items": [
                            {
                                "value": "60000",
                                "label": "示例控股集团",
                                "children": [
                                    {"value": "CHILD", "label": "示例技术部"}
                                ],
                            }
                        ],
                    },
                ]
            }
        }
    )

    assert catalog.category_codes_by_name == {"技术类": "11"}
    assert catalog.business_units_by_code == {
        "60000": "示例控股集团",
        "CHILD": "示例技术部",
    }


@pytest.mark.asyncio
async def test_alibaba_response_body_is_read_eagerly() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class FakePage:
        def on(self, _event: str, _callback: object) -> None:
            pass

    class FakeResponse:
        status = 200
        url = "https://campus-talent.alibaba.com/api/searchCondition/listBatch"

        async def json(self) -> dict[str, object]:
            started.set()
            await release.wait()
            return {"success": True, "content": {"graduate": []}}

    connector = AlibabaConnector(FakePage(), object())  # type: ignore[arg-type]
    connector._record_response(FakeResponse())  # type: ignore[arg-type]

    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    payload = await connector._next_payload(connector._batch_responses, "test")

    assert payload == {"success": True, "content": {"graduate": []}}


@pytest.mark.asyncio
async def test_alibaba_skips_replaced_unreadable_response() -> None:
    class FakePage:
        def on(self, _event: str, _callback: object) -> None:
            pass

    class FailedResponse:
        status = 200
        url = "https://campus-talent.alibaba.com/position/search"

        async def json(self) -> dict[str, object]:
            raise RuntimeError("response body was released during navigation")

    class ReplacementResponse:
        status = 200
        url = "https://campus-talent.alibaba.com/position/search"

        async def json(self) -> dict[str, object]:
            return {"success": True, "content": {"datas": []}}

    connector = AlibabaConnector(FakePage(), object())  # type: ignore[arg-type]
    connector._record_response(FailedResponse())  # type: ignore[arg-type]
    connector._record_response(ReplacementResponse())  # type: ignore[arg-type]

    payload = await connector._next_payload(connector._position_responses, "test")

    assert payload == {"success": True, "content": {"datas": []}}


@pytest.mark.asyncio
async def test_alibaba_retries_batch_page_only_for_unavailable_response() -> None:
    class FakePage:
        def on(self, _event: str, _callback: object) -> None:
            pass

    expected = (0, {"success": True}, source_catalog(), {"success": True})
    connector = AlibabaConnector(FakePage(), object())  # type: ignore[arg-type]
    connector._open_batch_once = AsyncMock(  # type: ignore[method-assign]
        side_effect=[BrowserResponseUnavailableError("missing"), expected]
    )

    actual = await connector._open_batch(RecruitmentBatch(101, "测试批次", "graduate"))

    assert actual == expected
    assert connector._open_batch_once.await_count == 2


@pytest.mark.asyncio
async def test_alibaba_batch_retry_is_bounded() -> None:
    class FakePage:
        def on(self, _event: str, _callback: object) -> None:
            pass

    connector = AlibabaConnector(FakePage(), object())  # type: ignore[arg-type]
    connector._open_batch_once = AsyncMock(  # type: ignore[method-assign]
        side_effect=BrowserResponseUnavailableError("missing")
    )

    with pytest.raises(BrowserResponseUnavailableError, match="did not load"):
        await connector._open_batch(RecruitmentBatch(101, "测试批次", "graduate"))

    assert connector._open_batch_once.await_count == BATCH_OPEN_ATTEMPTS
