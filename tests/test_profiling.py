from job_market.profiling import profile_source_fields
from job_market.schemas import Channel, JobRecord, LocationRecord


def make_record(external_id: str, source_payload: dict) -> JobRecord:
    return JobRecord(
        source_key="example_source",
        external_id=external_id,
        external_code=None,
        source_url=f"https://example.com/jobs/{external_id}",
        company_name="示例公司",
        channel=Channel.EXPERIENCED,
        employment_type_id="experienced",
        employment_type_name="社会招聘",
        title="示例岗位",
        description="示例描述",
        requirements="示例要求",
        locations=[LocationRecord(code="city:test", name="测试城")],
        source_payload=source_payload,
    )


def test_raw_field_profile_distinguishes_missing_null_empty_and_types() -> None:
    first = make_record(
        "1",
        {
            "degree": None,
            "experience": {"from": 3},
            "tags": [],
        },
    )
    second = make_record(
        "2",
        {
            "degree": "bachelor",
            "experience": None,
        },
    )

    stats = {item.field_path: item for item in profile_source_fields([first, second])}

    assert stats["degree"].row_count == 2
    assert stats["degree"].present_count == 2
    assert stats["degree"].non_null_count == 1
    assert stats["degree"].non_empty_count == 1
    assert stats["degree"].type_counts == {"null": 1, "string": 1}
    assert stats["experience.from"].present_count == 1
    assert stats["tags"].present_count == 1
    assert stats["tags"].non_empty_count == 0
