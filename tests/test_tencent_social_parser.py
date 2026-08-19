import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_market.connectors.tencent_social import TencentSocialConnector
from job_market.schemas import Channel

FIXTURE = Path(__file__).parent / "fixtures" / "tencent_social_job.json"


def test_tencent_social_job_preserves_direct_list_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = TencentSocialConnector.parse_job(raw)

    assert record.source_key == "tencent_social_cn"
    assert record.external_id == "synthetic-tencent-social-001"
    assert record.external_code == "120001"
    assert record.channel is Channel.EXPERIENCED
    assert record.description == "负责合成推荐系统研发。"
    assert record.requirements is None
    assert record.published_at is None
    assert record.source_updated_at == datetime(
        2026,
        8,
        18,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    assert record.experience_min_years == 3
    assert record.experience_max_years is None
    assert [(item.code, item.name, item.country_name) for item in record.locations] == [
        ("name:中国/测试城", "测试城", "中国")
    ]
    assert [(item.external_id, item.name) for item in record.categories] == [
        ("label:技术", "技术")
    ]
    assert [(item.code, item.name) for item in record.business_units] == [
        ("name:合成事业群", "合成事业群")
    ]
    assert record.source_payload == raw


def test_tencent_social_keeps_unknown_experience_text_only_in_raw() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["RequireWorkYearsName"] = "官网新增格式"

    record = TencentSocialConnector.parse_job(raw)

    assert record.experience_min_years is None
    assert record.source_payload["RequireWorkYearsName"] == "官网新增格式"


def test_tencent_social_keeps_missing_category_unclassified() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["CategoryName"] = None

    record = TencentSocialConnector.parse_job(raw)

    assert record.categories == []
    assert record.source_payload["CategoryName"] is None


def test_tencent_social_validates_final_page_shape() -> None:
    payload = {"Data": {"Count": 12, "Posts": [{"PostId": "x"}] * 2}}

    page = TencentSocialConnector._position_page(payload, expected_page=2)

    assert page["total"] == 12
    assert len(page["rows"]) == 2


def test_tencent_social_rejects_invalid_update_date() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["LastUpdateTime"] = "2026-08-18"

    with pytest.raises(ValueError, match="LastUpdateTime is invalid"):
        TencentSocialConnector.parse_job(raw)
