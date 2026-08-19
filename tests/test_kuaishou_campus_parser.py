from job_market.connectors.kuaishou_campus import (
    CategoryInfo,
    KuaishouCampusConnector,
    ProjectInfo,
    _select_project,
)
from job_market.schemas import Channel


def test_select_project_uses_latest_official_dictionary_entry() -> None:
    projects = [
        {"code": "older", "name": "2026应届生", "ifActive": True, "sortId": 8},
        {"code": "current", "name": "2027应届生", "ifActive": True, "sortId": 11},
        {"code": "intern", "name": "2027实习生", "ifActive": True, "sortId": 10},
    ]

    selected = _select_project(projects, Channel.CAMPUS)

    assert selected.code == "current"
    assert selected.name == "2027应届生"
    assert selected.nature_code == "fulltime"


def test_parse_kuaishou_campus_preserves_hierarchical_category() -> None:
    raw = {
        "id": 1001,
        "code": "fixture-code",
        "name": "示例策略产品经理",
        "positionStatusCode": "Release",
        "recruitProjectCode": "schoolr",
        "recruitSubProjectCode": "current",
        "positionNatureCode": "fulltime",
        "positionCategoryCode": "child",
        "description": "负责示例产品",
        "positionDemand": "本科及以上",
        "releaseTime": "2026-08-01 09:00:00",
        "updateTime": 1785546000000,
        "recruitNumber": None,
        "workLocationDicts": [{"code": "beijing", "name": "北京"}],
    }
    project = ProjectInfo("current", "2027应届生", "fulltime", "全职")
    categories = {
        "child": CategoryInfo("child", "策略产品", "product", "产品类"),
    }
    locations = {"beijing": {"code": "beijing", "name": "北京"}}
    natures = {"fulltime": {"code": "fulltime", "name": "全职"}}

    record = KuaishouCampusConnector.parse_job(
        raw,
        project,
        categories,
        locations,
        natures,
    )

    assert record.external_id == "1001"
    assert record.channel is Channel.CAMPUS
    assert record.recruitment_project_name == "2027应届生"
    assert record.categories[0].name == "策略产品"
    assert record.categories[0].parent_name == "产品类"
    assert record.locations[0].name == "北京"
    assert record.source_payload == raw
