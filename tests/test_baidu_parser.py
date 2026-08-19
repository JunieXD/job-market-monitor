from job_market.connectors.baidu import BaiduConnector, _recruitment_count
from job_market.schemas import Channel


def test_parse_baidu_preserves_direct_source_fields() -> None:
    raw = {
        "postId": "post-fixture",
        "jobId": "job-fixture",
        "name": "示例研发工程师",
        "postType": "技术",
        "publishDate": "2026-08-01",
        "updateDate": "2026-08-02",
        "recruitNum": "3",
        "serviceCondition": "本科及以上",
        "workContent": "负责示例系统研发",
        "workPlace": "北京市、上海市",
        "projectType": "校招",
        "projectTypeCode": "1",
        "hotFlag": True,
    }

    record = BaiduConnector.parse_job(raw, Channel.CAMPUS)

    assert record.external_id == "post-fixture"
    assert record.external_code == "job-fixture"
    assert record.recruitment_count == 3
    assert [location.name for location in record.locations] == ["北京市", "上海市"]
    assert record.categories[0].name == "技术"
    assert record.recruitment_project_name == "校招"
    assert record.source_payload == raw


def test_baidu_zero_recruitment_count_means_unspecified() -> None:
    assert _recruitment_count("0") is None
    assert _recruitment_count("若干") is None


def test_baidu_rejects_transient_zero_total_with_rows() -> None:
    payload = {
        "status": "ok",
        "data": {
            "total": "0",
            "list": [{"postId": "unexpected"}],
            "pageNum": 1,
            "pageSize": 100,
            "pages": 0,
        },
    }

    try:
        BaiduConnector._position_page(payload, expected_page=1)
    except RuntimeError as exc:
        assert "row mismatch" in str(exc)
    else:
        raise AssertionError("inconsistent Baidu pagination should fail")
