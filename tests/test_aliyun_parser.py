import json
from datetime import UTC, datetime
from pathlib import Path

from job_market.connectors.aliyun import AliyunConnector
from job_market.schemas import CategoryAssignmentMethod, Channel

FIXTURE = Path(__file__).parent / "fixtures" / "aliyun_job.json"


def test_aliyun_job_preserves_direct_source_facts() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    record = AliyunConnector.parse_job(raw)

    assert record.source_key == "alibaba_cloud_social"
    assert record.company_name == "阿里云"
    assert record.channel is Channel.EXPERIENCED
    assert record.external_id == "synthetic-aliyun-001"
    assert record.external_code == "GP-SYNTHETIC-001"
    assert str(record.source_url) == (
        "https://careers.aliyun.com/off-campus/position-detail"
        "?positionId=synthetic-aliyun-001"
    )
    assert record.description == "负责合成云平台研发。"
    assert record.requirements == "具备扎实的软件工程能力。"
    assert record.published_at == datetime.fromtimestamp(1786982400, UTC)
    assert record.source_updated_at == datetime.fromtimestamp(1786986000, UTC)
    assert record.degree_code == "bachelor"
    assert (record.experience_min_years, record.experience_max_years) == (3, 5)
    assert record.department_name == "云智能集团"
    assert [(item.code, item.name) for item in record.locations] == [
        ("city:杭州", "杭州"),
        ("city:上海", "上海"),
    ]
    categories = [
        (item.external_id, item.name, item.assignment_method)
        for item in record.categories
    ]
    assert categories == [
        ("category:130", "技术类", CategoryAssignmentMethod.DIRECT_FIELD)
    ]
    assert record.source_payload == raw


def test_aliyun_accepts_known_broken_pagination_metadata() -> None:
    payload = {
        "content": {
            "datas": [{"id": str(index)} for index in range(8)],
            "totalCount": 668,
            "pageSize": 500,
            "currentPage": 1,
        }
    }

    page = AliyunConnector._position_page(payload, expected_page=67)

    assert page["total"] == 668
    assert len(page["rows"]) == 8
