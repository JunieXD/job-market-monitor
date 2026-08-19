import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.kuaishou import (
    PORTALS,
    KuaishouConnector,
    SourceDictionaries,
)
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "kuaishou_job.json"

DICTIONARIES = SourceDictionaries(
    locations={
        "Beijing": {"code": "Beijing", "name": "北京", "parentCode": "domestic"},
        "Shanghai": {"code": "Shanghai", "name": "上海", "parentCode": "domestic"},
    },
    categories={"B009": {"code": "B009", "name": "工程类"}},
    experiences={"5": {"code": "5", "name": "3-5年"}},
)


def test_kuaishou_job_uses_official_dictionaries_and_direct_fields() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = KuaishouConnector.parse_job(
        raw,
        PORTALS[Channel.EXPERIENCED],
        DICTIONARIES,
    )

    assert record.source_key == "kuaishou_cn"
    assert record.external_id == "900001"
    assert record.external_code == "KS-FIXTURE-001"
    assert record.employment_type_id == "C001"
    assert record.employment_type_name == "社会招聘"
    assert record.recruitment_project_id == "socialr"
    assert record.recruitment_count == 3
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_code == "department-example"
    assert record.department_name == "示例研发部门"
    assert record.source_status == "online"
    assert record.is_hot is True
    assert record.published_at == datetime(
        2026,
        4,
        13,
        9,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.source_updated_at == datetime(
        2026,
        4,
        16,
        18,
        20,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [item.name for item in record.locations] == ["北京", "上海"]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("B009", "工程类")
    ]
    assert str(record.source_url).endswith("/official/social/job-info/900001")
    assert record.source_payload == raw


def test_kuaishou_internship_rejects_social_job() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="expected 'C002'"):
        KuaishouConnector.parse_job(
            raw,
            PORTALS[Channel.INTERNSHIP],
            DICTIONARIES,
        )


def test_kuaishou_rejects_unknown_source_category() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["positionCategoryCode"] = "unknown"

    with pytest.raises(ValueError, match="unknown category code"):
        KuaishouConnector.parse_job(
            raw,
            PORTALS[Channel.EXPERIENCED],
            DICTIONARIES,
        )


def test_kuaishou_validates_pagination_metadata_and_final_page_size() -> None:
    payload = {
        "result": {
            "total": 11,
            "list": [{}],
            "pageNum": 2,
            "pageSize": 10,
            "pages": 2,
        }
    }

    page = KuaishouConnector._position_page(payload, expected_page=2)

    assert page["total"] == 11
    assert len(page["rows"]) == 1


def test_kuaishou_accepts_null_list_only_for_empty_partition() -> None:
    payload = {
        "result": {
            "total": 0,
            "list": None,
            "pageNum": 1,
            "pageSize": 10,
            "pages": 0,
        }
    }

    assert KuaishouConnector._position_page(payload, expected_page=1)["rows"] == []


def test_kuaishou_rejects_duplicate_dictionary_codes() -> None:
    payload = {
        "code": 0,
        "result": {
            "workLocation": [
                {"code": "same", "name": "甲"},
                {"code": "same", "name": "乙"},
            ],
            "positionCategory": [{"code": "category", "name": "类别"}],
            "positionExperience": [{"code": "experience", "name": "不限"}],
        },
    }

    with pytest.raises(RuntimeError, match="repeated code"):
        KuaishouConnector.parse_dictionaries(payload)


def test_kuaishou_active_categories_must_match_dictionary() -> None:
    payload = {
        "code": 0,
        "result": {
            "category": [
                {"code": "B009", "name": "工程类"},
            ]
        },
    }

    labels = KuaishouConnector.parse_labels(payload, DICTIONARIES)

    assert labels.category_codes == ("B009",)
