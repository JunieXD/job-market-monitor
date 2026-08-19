import json
from pathlib import Path

from job_market.connectors.lenovo import LenovoConnector
from job_market.schemas import CategoryAssignmentMethod, Channel

FIXTURE = Path(__file__).parent / "fixtures" / "lenovo_job.json"


def test_lenovo_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = LenovoConnector.parse_job(
        raw,
        project_name="应届生招聘",
        city_names={"1": "北京", "6": "天津"},
        degree_names={"2": "本科"},
    )

    assert record.source_key == "lenovo_campus_cn"
    assert record.company_name == "联想集团"
    assert record.channel is Channel.CAMPUS
    assert record.external_id == "900001"
    assert record.employment_type_id == "1"
    assert record.employment_type_name == "应届生招聘"
    assert record.recruitment_project_id == "1"
    assert record.description == "负责合成产品规划。\n推动方案落地。"
    assert record.requirements == "本科及以上学历。\n具备协作能力。"
    assert record.degree_code == "2"
    assert record.degree_name == "本科"
    assert record.is_hot is True
    assert [(item.code, item.name) for item in record.locations] == [
        ("1", "北京"),
        ("6", "天津"),
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("name:China Geo", "China Geo"),
        ("name:IDG", "IDG"),
    ]
    categories = [
        (item.external_id, item.name, item.assignment_method)
        for item in record.categories
    ]
    assert categories == [
        ("label:产品策划类", "产品策划类", CategoryAssignmentMethod.DIRECT_FIELD)
    ]
    assert record.source_payload == raw


def test_lenovo_validates_terminal_page_shape() -> None:
    payload = {"code": 0, "result": {"total": 85, "rows": [{"id": "x"}] * 5}}

    page = LenovoConnector._position_page(payload, expected_page=9)

    assert page["total"] == 85
    assert len(page["rows"]) == 5
