import json
from datetime import UTC, datetime
from pathlib import Path

from job_market.connectors.alibaba_taotian import AlibabaTaoTianConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "alibaba_taotian_job.json"


def test_taotian_job_keeps_direct_facts_without_inventing_category() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AlibabaTaoTianConnector.parse_job(raw)

    assert record.source_key == "alibaba_taotian_social"
    assert record.company_name == "阿里巴巴集团"
    assert record.channel is Channel.EXPERIENCED
    assert str(record.source_url) == (
        "https://talent.taotian.com/off-campus/position-detail"
        "?positionId=taotian-fixture-001"
    )
    assert record.published_at == datetime.fromtimestamp(1750000000, UTC)
    assert record.source_updated_at == datetime.fromtimestamp(1760000000, UTC)
    assert record.degree_code == "bachelor"
    assert record.degree_name is None
    assert record.experience_min_years == 3
    assert record.experience_max_years == 5
    assert record.department_name == "示例产品事业部"
    assert [item.name for item in record.locations] == ["测试城", "第二城市"]
    assert record.categories == []


def test_taotian_category_catalog_uses_only_official_top_level_filters() -> None:
    categories = AlibabaTaoTianConnector._parse_categories(
        {
            "content": [
                {
                    "code": "130",
                    "name": "技术类",
                    "categories": [{"code": "136", "name": "开发"}],
                },
                {"code": "97", "name": "产品类", "categories": []},
            ]
        }
    )

    assert [(item.code, item.name) for item in categories] == [
        ("130", "技术类"),
        ("97", "产品类"),
    ]
    assert categories[0].assignment().assignment_method.value == "filter_membership"
