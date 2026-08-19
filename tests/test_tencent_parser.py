import json
from pathlib import Path

import pytest

from job_market.connectors.tencent import TencentConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "tencent_job.json"


def _connector_without_browser() -> TencentConnector:
    return TencentConnector.__new__(TencentConnector)


def test_tencent_detail_preserves_project_business_groups_and_interview_cities() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = TencentConnector.parse_job(raw)

    assert record.source_key == "tencent_cn"
    assert record.channel is Channel.CAMPUS
    assert record.external_id == "tencent-fixture-001"
    assert record.external_code == "6001"
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "应届毕业生"
    assert record.recruitment_project_id == "8001"
    assert record.recruitment_project_name == "示例校园招聘"
    assert record.interview_location_names == ["远程面试", "测试城"]
    assert [(item.code, item.name) for item in record.locations] == [
        ("1", "测试城"),
        ("3", "第二城市"),
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("7001", "示例事业群")
    ]
    assert record.categories[0].external_id == "2"
    assert record.categories[0].name == "技术"
    assert record.source_payload == raw


def test_tencent_rejects_more_rows_than_the_official_page_size() -> None:
    payload = {"data": {"positionList": [{}] * 11, "count": 11}}

    with pytest.raises(RuntimeError, match="exceeds UI page size"):
        TencentConnector._search_page(payload, expected_page=1)


def test_tencent_preserves_job_without_optional_text_or_location() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw.pop("desc")
    raw.pop("request")
    raw.pop("workCityList")

    record = TencentConnector.parse_job(raw)

    assert record.description is None
    assert record.requirements is None
    assert record.locations == []


def test_tencent_explicitly_removed_detail_is_not_a_collection_failure() -> None:
    record = _connector_without_browser()._parse_detail_response(
        {"message": "岗位已下架", "status": 404, "data": None},
        "removed-job",
    )

    assert record is None


def test_tencent_active_detail_is_validated_and_parsed() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    connector = _connector_without_browser()
    parsed_ids: list[str] = []
    parse_job = connector.parse_job

    def tracked_parse_job(payload):
        record = parse_job(payload)
        parsed_ids.append(record.external_id)
        return record

    connector.parse_job = tracked_parse_job
    record = connector._parse_detail_response(
        {"message": "", "status": 0, "data": raw},
        raw["postId"],
    )

    assert record is not None
    assert record.external_id == raw["postId"]
    assert parsed_ids == [raw["postId"]]


def test_tencent_project_internship_uses_official_id_when_post_id_is_null() -> None:
    raw = {
        "postId": None,
        "id": -2,
        "title": "项目实习生-技术",
        "desc": "项目制技术工作支持",
        "request": "具备编程能力",
        "recruitType": 2,
        "recruitLabelName": "日常实习",
        "projectId": 12,
        "projectName": "项目实习生",
        "tid": 2,
        "tidName": "技术",
    }

    record = _connector_without_browser()._parse_detail_response(
        {"message": "", "status": 0, "data": raw},
        "-2",
    )

    assert record is not None
    assert record.external_id == "-2"
    assert record.external_code == "-2"
    assert record.title == "项目实习生-技术"


def test_tencent_unknown_detail_error_remains_a_failure() -> None:
    with pytest.raises(RuntimeError, match="Invalid Tencent detail"):
        _connector_without_browser()._parse_detail_response(
            {"message": "系统繁忙", "status": 500, "data": None},
            "unavailable-job",
        )
