import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Channel(StrEnum):
    EXPERIENCED = "experienced"
    CAMPUS = "campus"
    INTERNSHIP = "internship"
    GENERAL = "general"


class CategoryAssignmentMethod(StrEnum):
    DIRECT_FIELD = "direct_field"
    FILTER_MEMBERSHIP = "filter_membership"


# Bump only when the normalized source-fact representation changes in a way
# that cannot be reconstructed from older stored job versions.
SOURCE_FACT_CONTRACT_VERSION = "v4"


class LocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    country_code: str | None = None
    country_name: str | None = None
    state_code: str | None = None
    state_name: str | None = None
    district_code: str | None = None
    district_name: str | None = None
    address: str | None = None


class BusinessUnitRecord(BaseModel):
    """A business/organization label explicitly supplied by a source site."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)


class SourceCategoryRecord(BaseModel):
    """A source category assigned by a field or by official filter membership."""

    model_config = ConfigDict(frozen=True)

    external_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    parent_external_id: str | None = Field(default=None, min_length=1)
    parent_name: str | None = Field(default=None, min_length=1)
    assignment_method: CategoryAssignmentMethod

    @model_validator(mode="after")
    def validate_parent(self) -> "SourceCategoryRecord":
        if (self.parent_external_id is None) != (self.parent_name is None):
            raise ValueError("source category parent id and name must be supplied together")
        return self


class JobRecord(BaseModel):
    """Only facts directly exposed by the source website belong here."""

    source_key: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    external_code: str | None = Field(default=None, min_length=1)
    source_url: HttpUrl
    company_name: str = Field(min_length=1)
    channel: Channel
    employment_type_id: str = Field(min_length=1)
    employment_type_name: str = Field(min_length=1)
    recruitment_project_id: str | None = None
    recruitment_project_name: str | None = None
    title: str = Field(min_length=1)
    description: str | None = Field(default=None, min_length=1)
    requirements: str | None = Field(default=None, min_length=1)
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    source_status: str | None = Field(default=None, min_length=1)
    recruitment_count: int | None = Field(default=None, ge=0)
    degree_code: str | None = Field(default=None, min_length=1)
    degree_name: str | None = Field(default=None, min_length=1)
    experience_min_years: int | None = Field(default=None, ge=0)
    experience_max_years: int | None = Field(default=None, ge=0)
    graduation_start_at: datetime | None = None
    graduation_end_at: datetime | None = None
    department_code: str | None = Field(default=None, min_length=1)
    department_name: str | None = Field(default=None, min_length=1)
    interview_location_names: list[str] = Field(default_factory=list)
    is_hot: bool | None = None
    locations: list[LocationRecord] = Field(default_factory=list)
    categories: list[SourceCategoryRecord] = Field(default_factory=list)
    # This is a source fact, not a project taxonomy or an LLM-derived topic.
    # Sources that do not expose business units leave the list empty.
    business_units: list[BusinessUnitRecord] = Field(default_factory=list)
    source_payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_ranges_and_dimensions(self) -> "JobRecord":
        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_min_years > self.experience_max_years
        ):
            raise ValueError("experience_min_years must not exceed experience_max_years")
        if (
            self.graduation_start_at is not None
            and self.graduation_end_at is not None
            and self.graduation_start_at > self.graduation_end_at
        ):
            raise ValueError("graduation_start_at must not exceed graduation_end_at")
        self._require_unique(self.locations, "code", "location")
        self._require_unique(self.categories, "external_id", "source category")
        self._require_unique(self.business_units, "code", "business unit")
        if len(set(self.interview_location_names)) != len(
            self.interview_location_names
        ):
            raise ValueError("interview locations must be unique")
        return self

    @staticmethod
    def _require_unique(items: list[Any], field: str, label: str) -> None:
        values = [getattr(item, field) for item in items]
        if len(set(values)) != len(values):
            raise ValueError(f"{label} {field}s must be unique")

    def content_hash(self) -> str:
        # A source's update timestamp may move without a visible content change.
        # Source status remains part of the hash because a status transition is
        # meaningful historical content.
        payload = self.model_dump(
            mode="json",
            exclude={"source_payload", "source_updated_at"},
        )
        categories = payload.pop("categories")
        if (
            len(categories) == 1
            and categories[0]["assignment_method"]
            == CategoryAssignmentMethod.DIRECT_FIELD.value
        ):
            # Preserve the pre-v3 hash shape for sources whose category model
            # remains one direct category. This prevents a schema migration
            # from becoming a false content-change event for every ByteDance
            # job.
            category = categories[0]
            payload["category_id"] = category["external_id"]
            payload["category_name"] = category["name"]
            payload["category_parent_id"] = category["parent_external_id"]
            payload["category_parent_name"] = category["parent_name"]
        elif categories:
            payload["categories"] = sorted(
                categories,
                key=lambda item: (item["external_id"], item["assignment_method"]),
            )
        # Keep hashes stable for sources that do not expose this optional
        # dimension.  Introducing the field must not make every existing
        # ByteDance job look changed merely because it now carries ``[]``.
        if not payload.get("business_units"):
            payload.pop("business_units", None)
        for field in (
            "degree_code",
            "degree_name",
            "experience_min_years",
            "experience_max_years",
            "graduation_start_at",
            "graduation_end_at",
            "department_code",
            "department_name",
            "recruitment_count",
        ):
            if payload.get(field) is None:
                payload.pop(field, None)
        if not payload.get("interview_location_names"):
            payload.pop("interview_location_names", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawSnapshotRecord(BaseModel):
    path: str
    sha256: str
    byte_size: int
    channel: Channel
    partition: str
    offset: int
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CollectionIssue(BaseModel):
    """A bounded, structured description of incomplete source coverage."""

    model_config = ConfigDict(frozen=True)

    scope: str = Field(pattern="^(source|partition|page|job)$")
    error_type: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    partition: str | None = Field(default=None, min_length=1, max_length=500)
    page: int | None = Field(default=None, ge=1)
    external_id: str | None = Field(default=None, min_length=1, max_length=300)
    retry_count: int = Field(default=0, ge=0)


class CollectionResult(BaseModel):
    channel: Channel
    jobs: list[JobRecord]
    snapshots: list[RawSnapshotRecord]
    partition_counts: dict[str, int]
    pages_fetched: int
    complete: bool
    # Live source lists can change while pagination is in progress. A result
    # may still contain valid observations without being safe evidence that
    # every previously known job absent from the walk has disappeared.
    absence_authoritative: bool = True
    issues: list[CollectionIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def incomplete_results_cannot_prove_absence(self) -> "CollectionResult":
        if not self.complete:
            self.absence_authoritative = False
        if len(self.issues) > 100:
            self.issues = [
                *self.issues[:99],
                CollectionIssue(
                    scope="source",
                    error_type="IssueLimitReached",
                    message="More collection issues were observed but omitted from storage",
                ),
            ]
        return self

    @property
    def outcome(self) -> str:
        if self.complete and self.absence_authoritative:
            return "success"
        return "partial"


class SourceFieldStatRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_path: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    present_count: int = Field(ge=0)
    non_null_count: int = Field(ge=0)
    non_empty_count: int = Field(ge=0)
    type_counts: dict[str, int]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class TopicMentionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_version_id: int = Field(gt=0)
    topic_key: str = Field(min_length=1)
    taxonomy_version: str = Field(default="v1", min_length=1)
    relevance: str = Field(pattern="^(primary|related)$")
    confidence: float = Field(ge=0, le=1)
    matched_fields: list[str] = Field(min_length=1)
    evidence: list[EvidenceRecord] = Field(min_length=1)


class DerivedAttributeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_version_id: int = Field(gt=0)
    attribute_key: str = Field(min_length=1)
    value: Any
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceRecord] = Field(min_length=1)


class CategoryMappingRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_category_id: int = Field(gt=0)
    canonical_category_key: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    mapping_method: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class LocationMappingRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    location_id: int = Field(gt=0)
    canonical_location_key: str = Field(min_length=1)
    mapping_method: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
