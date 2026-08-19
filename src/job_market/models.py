from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(String(100), nullable=False)
    scope_name: Mapped[str] = mapped_column(String(300), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Shanghai", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SourceChannel(Base):
    __tablename__ = "source_channels"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_source_channels_status",
        ),
    )

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    channel: Mapped[str] = mapped_column(String(30), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    coverage_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceBusinessUnit(Base):
    """A versioned business-unit fact exposed by one recruitment source."""

    __tablename__ = "source_business_units"
    __table_args__ = (
        Index("ix_source_business_units_code_history", "source_id", "external_code"),
        Index(
            "uq_source_business_units_one_current",
            "source_id",
            "external_code",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_code: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        Index("ix_crawl_runs_source_channel_date", "source_id", "channel", "snapshot_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    partition_counts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    absence_authoritative: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)


class CrawlRunFieldStat(Base):
    __tablename__ = "crawl_run_field_stats"
    __table_args__ = (
        CheckConstraint("row_count >= 0", name="ck_field_stats_row_count"),
        CheckConstraint(
            "present_count >= 0 AND present_count <= row_count",
            name="ck_field_stats_present_count",
        ),
        CheckConstraint(
            "non_null_count >= 0 AND non_null_count <= present_count",
            name="ck_field_stats_non_null_count",
        ),
        CheckConstraint(
            "non_empty_count >= 0 AND non_empty_count <= non_null_count",
            name="ck_field_stats_non_empty_count",
        ),
    )

    crawl_run_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_runs.id"), primary_key=True
    )
    field_path: Mapped[str] = mapped_column(String(500), primary_key=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    present_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_null_count: Mapped[int] = mapped_column(Integer, nullable=False)
    non_empty_count: Mapped[int] = mapped_column(Integer, nullable=False)
    type_counts: Mapped[dict] = mapped_column(JSON, nullable=False)


class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "channel",
            "snapshot_date",
            name="uq_daily_snapshots_source_channel_date",
        ),
        UniqueConstraint("crawl_run_id", name="uq_daily_snapshots_run"),
        Index("ix_daily_snapshots_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    crawl_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_source_channel_status", "source_id", "channel", "status"),
        CheckConstraint(
            "experience_min_years IS NULL OR experience_max_years IS NULL "
            "OR experience_min_years <= experience_max_years",
            name="ck_jobs_experience_range",
        ),
        CheckConstraint(
            "graduation_start_at IS NULL OR graduation_end_at IS NULL "
            "OR graduation_start_at <= graduation_end_at",
            name="ck_jobs_graduation_range",
        ),
        CheckConstraint(
            "recruitment_count IS NULL OR recruitment_count >= 0",
            name="ck_jobs_recruitment_count",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    external_code: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    employment_type_id: Mapped[str] = mapped_column(String(100), nullable=False)
    employment_type_name: Mapped[str] = mapped_column(String(100), nullable=False)
    recruitment_project_id: Mapped[str | None] = mapped_column(String(100))
    recruitment_project_name: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_status: Mapped[str | None] = mapped_column(String(100))
    recruitment_count: Mapped[int | None] = mapped_column(Integer)
    degree_code: Mapped[str | None] = mapped_column(String(100))
    degree_name: Mapped[str | None] = mapped_column(String(300))
    experience_min_years: Mapped[int | None] = mapped_column(Integer)
    experience_max_years: Mapped[int | None] = mapped_column(Integer)
    graduation_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    graduation_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    department_code: Mapped[str | None] = mapped_column(String(200))
    department_name: Mapped[str | None] = mapped_column(String(300))
    interview_location_names: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    category_id: Mapped[str | None] = mapped_column(String(100))
    category_name: Mapped[str | None] = mapped_column(String(300))
    category_parent_id: Mapped[str | None] = mapped_column(String(100))
    category_parent_name: Mapped[str | None] = mapped_column(String(300))
    is_hot: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    missing_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    missing_since_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_canonical_seen_on: Mapped[date | None] = mapped_column(Date)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceCategory(Base):
    __tablename__ = "source_categories"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_categories_external"),
        Index("ix_source_categories_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("source_categories.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobVersion(Base):
    __tablename__ = "job_versions"
    __table_args__ = (
        UniqueConstraint("job_id", "content_hash", name="uq_job_versions_hash"),
        Index("ix_job_versions_job_observed", "job_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    crawl_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    source_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_categories.id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_contract_version: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobObservation(Base):
    __tablename__ = "job_observations"
    __table_args__ = (
        UniqueConstraint("job_id", "crawl_run_id", name="uq_job_observation_run"),
        Index("ix_job_observations_run", "crawl_run_id"),
        Index("ix_job_observations_version", "job_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    job_version_id: Mapped[int] = mapped_column(ForeignKey("job_versions.id"), nullable=False)
    crawl_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("source_id", "code", name="uq_locations_source_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(100))
    country_name: Mapped[str | None] = mapped_column(String(300))
    state_code: Mapped[str | None] = mapped_column(String(100))
    state_name: Mapped[str | None] = mapped_column(String(300))
    district_code: Mapped[str | None] = mapped_column(String(100))
    district_name: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(Text)


class JobLocation(Base):
    __tablename__ = "job_locations"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), primary_key=True)


class JobVersionLocation(Base):
    __tablename__ = "job_version_locations"
    __table_args__ = (
        Index("ix_job_version_locations_canonical", "canonical_location_id"),
    )

    job_version_id: Mapped[int] = mapped_column(
        ForeignKey("job_versions.id"), primary_key=True
    )
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), primary_key=True)
    canonical_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_locations.id")
    )
    mapping_method: Mapped[str | None] = mapped_column(String(30))
    mapping_version: Mapped[str | None] = mapped_column(String(100))
    mapping_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class JobVersionBusinessUnit(Base):
    __tablename__ = "job_version_business_units"

    job_version_id: Mapped[int] = mapped_column(
        ForeignKey("job_versions.id"), primary_key=True
    )
    source_business_unit_id: Mapped[int] = mapped_column(
        ForeignKey("source_business_units.id"), primary_key=True
    )


class JobVersionSourceCategory(Base):
    __tablename__ = "job_version_source_categories"
    __table_args__ = (
        CheckConstraint(
            "assignment_method IN ('direct_field', 'filter_membership')",
            name="ck_job_version_source_categories_method",
        ),
        Index(
            "ix_job_version_source_categories_category",
            "source_category_id",
            "job_version_id",
        ),
        Index(
            "ix_job_version_source_categories_canonical",
            "canonical_category_id",
        ),
    )

    job_version_id: Mapped[int] = mapped_column(
        ForeignKey("job_versions.id"), primary_key=True
    )
    source_category_id: Mapped[int] = mapped_column(
        ForeignKey("source_categories.id"), primary_key=True
    )
    assignment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    canonical_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_categories.id")
    )
    mapping_method: Mapped[str | None] = mapped_column(String(30))
    mapping_version: Mapped[str | None] = mapped_column(String(100))
    mapping_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class JobLifecycleEvent(Base):
    __tablename__ = "job_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('first_seen', 'changed', 'missing', 'recovered', 'closed', "
            "'reopened', 'enriched')",
            name="ck_job_lifecycle_event_type",
        ),
        UniqueConstraint(
            "job_id", "crawl_run_id", "event_type", name="uq_job_lifecycle_event_run"
        ),
        Index("ix_job_lifecycle_events_type_observed", "event_type", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    crawl_run_id: Mapped[str | None] = mapped_column(ForeignKey("crawl_runs.id"))
    job_version_id: Mapped[int | None] = mapped_column(ForeignKey("job_versions.id"))
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class CanonicalCategory(Base):
    __tablename__ = "canonical_categories"
    __table_args__ = (
        UniqueConstraint("taxonomy_version", "key", name="uq_canonical_categories_version_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_categories.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CategoryMapping(Base):
    __tablename__ = "category_mappings"
    __table_args__ = (
        UniqueConstraint(
            "source_category_id",
            "mapping_version",
            name="uq_category_mappings_source_version",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_category_confidence"),
        Index("ix_category_mappings_current", "source_category_id", "is_current"),
        Index(
            "uq_category_mappings_one_current",
            "source_category_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_category_id: Mapped[int] = mapped_column(
        ForeignKey("source_categories.id"), nullable=False
    )
    canonical_category_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_categories.id"), nullable=False
    )
    mapping_method: Mapped[str] = mapped_column(String(30), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(100), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CanonicalLocation(Base):
    __tablename__ = "canonical_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    level: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(100))
    country_name: Mapped[str | None] = mapped_column(String(300))
    state_code: Mapped[str | None] = mapped_column(String(100))
    state_name: Mapped[str | None] = mapped_column(String(300))
    city_name: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceLocationMapping(Base):
    __tablename__ = "source_location_mappings"
    __table_args__ = (
        UniqueConstraint(
            "location_id",
            "mapping_version",
            name="uq_source_location_mappings_version",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_location_confidence"),
        Index("ix_source_location_mappings_current", "location_id", "is_current"),
        Index(
            "uq_source_location_mappings_one_current",
            "location_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    canonical_location_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_locations.id"), nullable=False
    )
    mapping_method: Mapped[str] = mapped_column(String(30), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(100), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivationRun(Base):
    __tablename__ = "derivation_runs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('rule', 'llm', 'manual')",
            name="ck_derivation_run_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="ck_derivation_run_status",
        ),
        Index(
            "ix_derivation_runs_current",
            "extractor_name",
            "extractor_version",
            "is_current",
        ),
        Index(
            "uq_derivation_runs_one_current",
            "extractor_name",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    extractor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("taxonomy_version", "key", name="uq_topics_version_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TopicAlias(Base):
    __tablename__ = "topic_aliases"
    __table_args__ = (
        UniqueConstraint("topic_id", "normalized_alias", name="uq_topic_aliases_normalized"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(300), nullable=False)
    match_mode: Mapped[str] = mapped_column(String(30), default="phrase", nullable=False)


class JobTopicMention(Base):
    __tablename__ = "job_topic_mentions"
    __table_args__ = (
        UniqueConstraint(
            "job_version_id",
            "topic_id",
            "derivation_run_id",
            name="uq_job_topic_mentions_run",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_topic_confidence"),
        CheckConstraint(
            "relevance IN ('primary', 'related')",
            name="ck_topic_relevance",
        ),
        Index("ix_job_topic_mentions_topic_version", "topic_id", "job_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_version_id: Mapped[int] = mapped_column(ForeignKey("job_versions.id"), nullable=False)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    derivation_run_id: Mapped[str] = mapped_column(
        ForeignKey("derivation_runs.id"), nullable=False
    )
    relevance: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    matched_fields: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobDerivedAttribute(Base):
    __tablename__ = "job_derived_attributes"
    __table_args__ = (
        UniqueConstraint(
            "job_version_id",
            "attribute_key",
            "derivation_run_id",
            name="uq_job_derived_attributes_run",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_attribute_confidence"),
        Index("ix_job_derived_attributes_key", "attribute_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_version_id: Mapped[int] = mapped_column(ForeignKey("job_versions.id"), nullable=False)
    attribute_key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=False
    )
    derivation_run_id: Mapped[str] = mapped_column(
        ForeignKey("derivation_runs.id"), nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_run_id: Mapped[str] = mapped_column(ForeignKey("crawl_runs.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(1500), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    partition: Mapped[str] = mapped_column(String(500), nullable=False)
    offset: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
