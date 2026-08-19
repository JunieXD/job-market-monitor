from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_market.schemas import (
    CategoryAssignmentMethod,
    Channel,
    CollectionIssue,
    CollectionResult,
    JobRecord,
    LocationRecord,
    SourceCategoryRecord,
)


def make_record() -> JobRecord:
    return JobRecord(
        source_key="bytedance_cn",
        external_id="1",
        external_code="A1",
        source_url="https://jobs.bytedance.com/experienced/position/1/detail",
        company_name="字节跳动",
        channel=Channel.EXPERIENCED,
        employment_type_id="101",
        employment_type_name="正式",
        title="示例职位",
        description="职位描述",
        requirements="职位要求",
        published_at=datetime.now(UTC),
        categories=[
            SourceCategoryRecord(
                external_id="cat",
                name="后端",
                assignment_method=CategoryAssignmentMethod.DIRECT_FIELD,
            )
        ],
        locations=[LocationRecord(code="CT_1", name="北京")],
        source_payload={"id": "1"},
    )


def test_content_hash_ignores_raw_payload() -> None:
    record = make_record()
    changed_raw = record.model_copy(update={"source_payload": {"server": "changed"}})

    assert record.content_hash() == changed_raw.content_hash()


def test_content_hash_ignores_source_update_time_but_tracks_source_status() -> None:
    record = make_record().model_copy(update={"source_status": "recruit"})
    changed_time = record.model_copy(
        update={"source_updated_at": datetime(2026, 8, 18, tzinfo=UTC)}
    )
    changed_status = record.model_copy(update={"source_status": "paused"})

    assert record.content_hash() == changed_time.content_hash()
    assert record.content_hash() != changed_status.content_hash()


def test_required_source_fields_are_not_silently_empty() -> None:
    payload = make_record().model_dump()
    payload["external_code"] = ""

    with pytest.raises(ValidationError):
        JobRecord.model_validate(payload)


def test_source_optional_fields_may_be_missing() -> None:
    record = make_record().model_copy(
        update={
            "external_code": None,
            "description": None,
            "requirements": None,
            "published_at": None,
            "is_hot": None,
        }
    )

    assert record.external_code is None
    assert record.description is None
    assert record.requirements is None
    assert record.published_at is None
    assert record.is_hot is None


def test_source_job_may_have_no_structured_location() -> None:
    record = make_record().model_copy(update={"locations": []})

    assert record.locations == []


def test_incomplete_collection_has_partial_outcome_and_bounded_issue() -> None:
    result = CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[make_record()],
        snapshots=[],
        partition_counts={"all": 2, "collected-unique": 1},
        pages_fetched=1,
        complete=False,
        issues=[
            CollectionIssue(
                scope="page",
                partition="root",
                page=2,
                error_type="PageUnavailable",
                message="request failed after retries",
                retry_count=2,
            )
        ],
    )

    assert result.outcome == "partial"
    assert result.absence_authoritative is False


def test_collection_issue_storage_is_bounded() -> None:
    issues = [
        CollectionIssue(
            scope="job",
            external_id=str(index),
            error_type="SyntheticError",
            message="synthetic",
        )
        for index in range(150)
    ]

    result = CollectionResult(
        channel=Channel.EXPERIENCED,
        jobs=[],
        snapshots=[],
        partition_counts={},
        pages_fetched=0,
        complete=False,
        issues=issues,
    )

    assert len(result.issues) == 100
    assert result.issues[-1].error_type == "IssueLimitReached"
