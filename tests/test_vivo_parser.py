import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_market.connectors.vivo import VivoConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "vivo_job.json"
CATEGORIES = {
    "category-root": ("研发类", None, None),
    "category-child": ("研发类", "category-root", "研发类"),
}


def test_vivo_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = VivoConnector.parse_job(raw, CATEGORIES)

    assert record.source_key == "vivo_social_cn"
    assert record.external_id == "synthetic-vivo-001"
    assert record.external_code == "SYN001"
    assert record.channel is Channel.EXPERIENCED
    assert record.description == "岗位职责与要求的官网合并正文。"
    assert record.requirements is None
    assert record.recruitment_count == 2
    assert record.degree_code == "BACHELOR_ABOVE"
    assert record.degree_name == "本科"
    assert (record.experience_min_years, record.experience_max_years) == (5, None)
    assert record.department_name == "合成研发部"
    assert record.is_hot is True
    assert record.published_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert [(item.code, item.name, item.address) for item in record.locations] == [
        ("TEST_CITY", "测试城", "测试城研发中心"),
        ("SECOND_CITY", "第二城市", "第二城市研发中心"),
    ]
    category = record.categories[0]
    assert (category.external_id, category.name) == ("category-child", "研发类")
    assert (category.parent_external_id, category.parent_name) == (
        "category-root",
        "研发类",
    )
    assert record.source_payload == raw


def test_vivo_does_not_publish_fuzzy_recruitment_count() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["fuzzy_head_count"] = 1

    record = VivoConnector.parse_job(raw, CATEGORIES)

    assert record.recruitment_count is None
    assert record.source_payload["head_count"] == 2
    assert record.source_payload["fuzzy_head_count"] == 1


def test_vivo_category_catalog_preserves_parent_identity() -> None:
    payload = {
        "data": [
            {"id": "root", "name": "研发类", "parent_id": "M1"},
            {"id": "child", "name": "研发类", "parent_id": "root"},
        ]
    }

    catalog = VivoConnector.parse_category_catalog(payload)

    assert catalog["root"] == ("研发类", None, None)
    assert catalog["child"] == ("研发类", "root", "研发类")


def test_vivo_validates_final_page_metadata() -> None:
    payload = {
        "data": [{"job_id": "x"}] * 6,
        "meta": {"page": 58, "total": 576, "page_count": 58, "max_results": 10},
    }

    page = VivoConnector._position_page(payload, expected_page=58)

    assert page["total"] == 576
    assert len(page["rows"]) == 6
