from types import SimpleNamespace
from unittest.mock import AsyncMock

from job_market.connectors.didi import DidiConnector
from job_market.schemas import Channel, JobRecord, LocationRecord


def test_parse_didi_combines_list_identity_and_public_detail() -> None:
    row = {
        "jdId": 12345,
        "jdNo": "JD-FIXTURE",
        "jobType": 1,
        "jobName": "示例工程师 (JD-FIXTURE)",
        "workArea": "北京市",
        "deptName": "示例事业部",
        "refreshTime": "2026-08-02 10:00:00",
    }
    detail = {
        "jobName": "示例工程师",
        "deptName": "示例事业部",
        "publishTime": "2026-08-01 09:00:00",
        "refreshTime": "2026-08-02 10:00:00",
        "jdNo": "JD-FIXTURE",
        "recruitType": "1",
        "jobDesc": "负责示例系统研发",
        "qualification": "具备示例能力",
        "jdStatus": 2,
        "workArea": "北京市",
        "recruitNum": 2,
        "jobType": "技术",
    }

    record = DidiConnector.parse_job(row, detail, {"1": "技术"}, Channel.EXPERIENCED)

    assert record.external_id == "12345"
    assert record.external_code == "JD-FIXTURE"
    assert record.recruitment_count == 2
    assert record.department_name == "示例事业部"
    assert record.categories[0].external_id == "1"
    assert record.categories[0].name == "技术"
    assert record.locations[0].name == "北京市"


def test_didi_rejects_zero_total_with_nonempty_page() -> None:
    payload = {
        "data": {
            "total": 0,
            "items": [{"jdId": 12345}],
            "page": 1,
            "size": 16,
        }
    }

    try:
        DidiConnector._position_page(payload, expected_page=1)
    except RuntimeError as exc:
        assert "zero total" in str(exc)
    else:
        raise AssertionError("transient Didi total should trigger a retry")


async def test_didi_live_list_warning_is_not_authoritative_for_absence() -> None:
    page = SimpleNamespace(goto=AsyncMock())
    connector = DidiConnector(
        page,
        SimpleNamespace(didi_request_delay_seconds=0.5),
    )
    rows = {
        str(index): {"jdId": str(index)}
        for index in range(49)
    }
    connector._fetch_categories = AsyncMock(return_value={})
    connector._collect_partitioned_pass = AsyncMock(
        return_value=(rows, 50, {"root-before": 50, "root-after": 50}, False)
    )
    connector._fetch_detail = AsyncMock(return_value={})
    def parse_job(row, detail, categories, channel) -> JobRecord:
        return JobRecord(
            source_key="didi_social_cn",
            external_id=str(row["jdId"]),
            source_url=f"https://talent.didiglobal.com/social/detail/{row['jdId']}",
            company_name="滴滴",
            channel=channel,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=f"测试岗位 {row['jdId']}",
            locations=[LocationRecord(code="city:test", name="测试城")],
            source_payload=row,
        )

    connector.parse_job = parse_job

    result = await connector.collect(Channel.EXPERIENCED)

    assert result.complete is True
    assert result.absence_authoritative is False
    assert result.partition_counts["list-consistency-warning"] == 1
    assert result.partition_counts["list-missing-estimate"] == 1
    assert len(result.jobs) == 49


async def test_didi_uses_only_one_consistent_partition_pass_as_authoritative() -> None:
    page = SimpleNamespace(goto=AsyncMock())
    connector = DidiConnector(
        page,
        SimpleNamespace(didi_request_delay_seconds=0.5),
    )
    first = {str(index): {"jdId": str(index)} for index in range(49)}
    second = {str(index): {"jdId": str(index)} for index in range(50)}
    connector._fetch_categories = AsyncMock(return_value={})
    connector._collect_partitioned_pass = AsyncMock(
        side_effect=[
            (first, 50, {"root-before": 50, "root-after": 50}, False),
            (second, 50, {"root-before": 50, "root-after": 50}, True),
        ]
    )
    connector._fetch_detail = AsyncMock(return_value={})

    def parse_job(row, detail, categories, channel) -> JobRecord:
        return JobRecord(
            source_key="didi_social_cn",
            external_id=str(row["jdId"]),
            source_url=f"https://talent.didiglobal.com/social/detail/{row['jdId']}",
            company_name="滴滴",
            channel=channel,
            employment_type_id="experienced",
            employment_type_name="社会招聘",
            title=f"测试岗位 {row['jdId']}",
            locations=[LocationRecord(code="city:test", name="测试城")],
            source_payload=row,
        )

    connector.parse_job = parse_job

    result = await connector.collect(Channel.EXPERIENCED)

    assert result.complete is True
    assert result.absence_authoritative is True
    assert "list-consistency-warning" not in result.partition_counts
    assert len(result.jobs) == 50


async def test_didi_category_partitions_cover_root_exactly() -> None:
    page = SimpleNamespace()
    connector = DidiConnector(
        page,
        SimpleNamespace(didi_request_delay_seconds=0.5),
    )
    root_calls = 0

    def payload(total: int, items: list[dict]) -> dict:
        return {
            "meta": {"code": 0},
            "data": {"total": total, "items": items, "page": 1, "size": 16},
        }

    async def fetch(page_number: int, *, job_type: str | None = None) -> dict:
        nonlocal root_calls
        assert page_number == 1
        if job_type is None:
            root_calls += 1
            return payload(2, [{"jdId": "root", "jobType": 1}])
        return payload(1, [{"jdId": f"job-{job_type}", "jobType": int(job_type)}])

    connector._fetch_list_page = fetch

    rows, total, counts, consistent = await connector._collect_partitioned_pass(
        Channel.EXPERIENCED,
        {"1": "技术", "3": "产品"},
        attempt=1,
    )

    assert root_calls == 2
    assert total == 2
    assert consistent is True
    assert set(rows) == {"job-1", "job-3"}
    assert counts == {
        "root-before": 2,
        "category-1": 1,
        "category-3": 1,
        "root-after": 2,
        "category-sum": 2,
    }


async def test_didi_retries_only_category_with_page_drift(monkeypatch) -> None:
    connector = DidiConnector(
        SimpleNamespace(),
        SimpleNamespace(didi_request_delay_seconds=0.5),
    )
    calls: list[tuple[int, str | None]] = []

    def payload(page: int, items: list[dict]) -> dict:
        return {
            "meta": {"code": 0},
            "data": {"total": 17, "items": items, "page": page, "size": 16},
        }

    async def fetch(page_number: int, *, job_type: str | None = None) -> dict:
        calls.append((page_number, job_type))
        pass_number = 1 if len(calls) <= 2 else 2
        if page_number == 1:
            return payload(1, [
                {"jdId": str(index), "jobType": 1}
                for index in range(16)
            ])
        final_id = "15" if pass_number == 1 else "16"
        return payload(2, [{"jdId": final_id, "jobType": 1}])

    connector._fetch_list_page = fetch
    monkeypatch.setattr("job_market.connectors.didi.asyncio.sleep", AsyncMock())

    rows, total, consistent = await connector._collect_category_partition(
        Channel.EXPERIENCED,
        "1",
        collection_attempt=1,
    )

    assert consistent is True
    assert total == 17
    assert len(rows) == 17
    assert calls == [(1, "1"), (2, "1"), (1, "1"), (2, "1")]


async def test_didi_page_request_retries_transient_network_failure(monkeypatch) -> None:
    payload = {"meta": {"code": 0}, "data": {"ok": True}}
    page = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[RuntimeError("network-1"), RuntimeError("network-2"), {
                "status": 200,
                "payload": payload,
            }]
        )
    )
    connector = DidiConnector(
        page,
        SimpleNamespace(didi_request_delay_seconds=0.5),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("job_market.connectors.didi.asyncio.sleep", sleep)

    result = await connector._request_json("https://example.test/jobs", method="GET")

    assert result == payload
    assert page.evaluate.await_count == 3
    assert sleep.await_count >= 2
    assert page.evaluate.await_args_list[-1].args[1]["timeoutMs"] == 30_000
