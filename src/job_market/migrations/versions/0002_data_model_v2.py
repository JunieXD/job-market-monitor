"""Add version-linked observations and analysis dimensions.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _company_key(source_key: str) -> str:
    if source_key == "bytedance_cn":
        return "bytedance"
    return source_key


def _canonical_location_key(row: dict[str, Any]) -> str:
    identity = [
        str(row.get("country_code") or row.get("country_name") or "").strip().casefold(),
        str(row.get("state_code") or row.get("state_name") or "").strip().casefold(),
        str(row.get("name") or "").strip().casefold(),
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return f"city-{digest[:24]}"


def _create_tables() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("sources", sa.Column("company_id", sa.Integer(), nullable=True))
    op.add_column(
        "sources",
        sa.Column(
            "timezone",
            sa.String(100),
            nullable=False,
            server_default="Asia/Shanghai",
        ),
    )
    op.add_column("crawl_runs", sa.Column("snapshot_date", sa.Date(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("missing_since_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("first_canonical_seen_on", sa.Date(), nullable=True),
    )

    op.create_table(
        "daily_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("crawl_run_id", sa.String(36), sa.ForeignKey("crawl_runs.id"), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "channel",
            "snapshot_date",
            name="uq_daily_snapshots_source_channel_date",
        ),
        sa.UniqueConstraint("crawl_run_id", name="uq_daily_snapshots_run"),
    )
    op.create_index("ix_daily_snapshots_date", "daily_snapshots", ["snapshot_date"])

    op.create_table(
        "source_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("source_categories.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "external_id",
            name="uq_source_categories_external",
        ),
    )
    op.create_index("ix_source_categories_parent", "source_categories", ["parent_id"])
    op.add_column(
        "job_versions",
        sa.Column("source_category_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "job_observations",
        sa.Column("job_version_id", sa.Integer(), nullable=True),
    )
    op.create_table(
        "job_version_locations",
        sa.Column(
            "job_version_id",
            sa.Integer(),
            sa.ForeignKey("job_versions.id"),
            primary_key=True,
        ),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            primary_key=True,
        ),
    )
    op.create_table(
        "job_lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("crawl_run_id", sa.String(36), sa.ForeignKey("crawl_runs.id")),
        sa.Column("job_version_id", sa.Integer(), sa.ForeignKey("job_versions.id")),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('first_seen', 'changed', 'missing', 'recovered', 'closed', "
            "'reopened')",
            name="ck_job_lifecycle_event_type",
        ),
        sa.UniqueConstraint(
            "job_id",
            "crawl_run_id",
            "event_type",
            name="uq_job_lifecycle_event_run",
        ),
    )
    op.create_index(
        "ix_job_lifecycle_events_type_observed",
        "job_lifecycle_events",
        ["event_type", "observed_at"],
    )

    op.create_table(
        "canonical_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("taxonomy_version", sa.String(100), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("canonical_categories.id")),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "taxonomy_version",
            "key",
            name="uq_canonical_categories_version_key",
        ),
    )
    op.create_table(
        "category_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_category_id",
            sa.Integer(),
            sa.ForeignKey("source_categories.id"),
            nullable=False,
        ),
        sa.Column(
            "canonical_category_id",
            sa.Integer(),
            sa.ForeignKey("canonical_categories.id"),
            nullable=False,
        ),
        sa.Column("mapping_method", sa.String(30), nullable=False),
        sa.Column("mapping_version", sa.String(100), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_category_confidence",
        ),
        sa.UniqueConstraint(
            "source_category_id",
            "mapping_version",
            name="uq_category_mappings_source_version",
        ),
    )
    op.create_index(
        "ix_category_mappings_current",
        "category_mappings",
        ["source_category_id", "is_current"],
    )
    op.create_index(
        "uq_category_mappings_one_current",
        "category_mappings",
        ["source_category_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "canonical_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("level", sa.String(30), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("country_code", sa.String(100)),
        sa.Column("country_name", sa.String(300)),
        sa.Column("state_code", sa.String(100)),
        sa.Column("state_name", sa.String(300)),
        sa.Column("city_name", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "source_location_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            nullable=False,
        ),
        sa.Column(
            "canonical_location_id",
            sa.Integer(),
            sa.ForeignKey("canonical_locations.id"),
            nullable=False,
        ),
        sa.Column("mapping_method", sa.String(30), nullable=False),
        sa.Column("mapping_version", sa.String(100), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_location_confidence",
        ),
        sa.UniqueConstraint(
            "location_id",
            "mapping_version",
            name="uq_source_location_mappings_version",
        ),
    )
    op.create_index(
        "ix_source_location_mappings_current",
        "source_location_mappings",
        ["location_id", "is_current"],
    )
    op.create_index(
        "uq_source_location_mappings_one_current",
        "source_location_mappings",
        ["location_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "derivation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("extractor_name", sa.String(200), nullable=False),
        sa.Column("extractor_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(
            "kind IN ('rule', 'llm', 'manual')",
            name="ck_derivation_run_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="ck_derivation_run_status",
        ),
    )
    op.create_index(
        "ix_derivation_runs_current",
        "derivation_runs",
        ["extractor_name", "extractor_version", "is_current"],
    )
    op.create_index(
        "uq_derivation_runs_one_current",
        "derivation_runs",
        ["extractor_name"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("taxonomy_version", sa.String(100), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "taxonomy_version",
            "key",
            name="uq_topics_version_key",
        ),
    )
    op.create_table(
        "topic_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("alias", sa.String(300), nullable=False),
        sa.Column("normalized_alias", sa.String(300), nullable=False),
        sa.Column("match_mode", sa.String(30), nullable=False),
        sa.UniqueConstraint(
            "topic_id",
            "normalized_alias",
            name="uq_topic_aliases_normalized",
        ),
    )
    op.create_table(
        "job_topic_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_version_id",
            sa.Integer(),
            sa.ForeignKey("job_versions.id"),
            nullable=False,
        ),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column(
            "derivation_run_id",
            sa.String(36),
            sa.ForeignKey("derivation_runs.id"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("matched_fields", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_topic_confidence",
        ),
        sa.UniqueConstraint(
            "job_version_id",
            "topic_id",
            "derivation_run_id",
            name="uq_job_topic_mentions_run",
        ),
    )
    op.create_index(
        "ix_job_topic_mentions_topic_version",
        "job_topic_mentions",
        ["topic_id", "job_version_id"],
    )
    op.create_table(
        "job_derived_attributes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_version_id",
            sa.Integer(),
            sa.ForeignKey("job_versions.id"),
            nullable=False,
        ),
        sa.Column("attribute_key", sa.String(200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "derivation_run_id",
            sa.String(36),
            sa.ForeignKey("derivation_runs.id"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_attribute_confidence",
        ),
        sa.UniqueConstraint(
            "job_version_id",
            "attribute_key",
            "derivation_run_id",
            name="uq_job_derived_attributes_run",
        ),
    )
    op.create_index(
        "ix_job_derived_attributes_key",
        "job_derived_attributes",
        ["attribute_key"],
    )


def _backfill_companies(bind: sa.Connection, metadata: sa.MetaData, now: datetime) -> None:
    companies = sa.Table("companies", metadata, autoload_with=bind)
    sources = sa.Table("sources", metadata, autoload_with=bind)
    company_ids: dict[str, int] = {}
    for source in bind.execute(sa.select(sources)).mappings():
        key = _company_key(source["key"])
        company_id = company_ids.get(key)
        if company_id is None:
            company_id = bind.execute(
                companies.insert()
                .values(key=key, name=source["company_name"], created_at=now)
                .returning(companies.c.id)
            ).scalar_one()
            company_ids[key] = company_id
        bind.execute(
            sources.update().where(sources.c.id == source["id"]).values(company_id=company_id)
        )


def _backfill_runs_and_snapshots(
    bind: sa.Connection,
    metadata: sa.MetaData,
    now: datetime,
) -> None:
    sources = sa.Table("sources", metadata, autoload_with=bind)
    runs = sa.Table("crawl_runs", metadata, autoload_with=bind)
    snapshots = sa.Table("daily_snapshots", metadata, autoload_with=bind)
    timezones = {
        row.id: ZoneInfo(row.timezone)
        for row in bind.execute(sa.select(sources.c.id, sources.c.timezone))
    }
    successful: dict[tuple[int, str, Any], dict[str, Any]] = {}
    for row in bind.execute(sa.select(runs)).mappings():
        moment = _as_aware(row["finished_at"] or row["started_at"])
        snapshot_date = moment.astimezone(timezones[row["source_id"]]).date()
        bind.execute(
            runs.update().where(runs.c.id == row["id"]).values(snapshot_date=snapshot_date)
        )
        if row["status"] != "success" or not row["complete"]:
            continue
        key = (row["source_id"], row["channel"], snapshot_date)
        candidate = dict(row)
        candidate["snapshot_date"] = snapshot_date
        previous = successful.get(key)
        if previous is None:
            successful[key] = candidate
            continue
        candidate_time = _as_aware(candidate["finished_at"] or candidate["started_at"])
        previous_time = _as_aware(previous["finished_at"] or previous["started_at"])
        if (candidate_time, candidate["id"]) < (previous_time, previous["id"]):
            successful[key] = candidate

    earliest = {
        (source_id, channel): min(key[2] for key in successful if key[:2] == (source_id, channel))
        for source_id, channel, _ in successful
    }
    for (source_id, channel, snapshot_date), run in successful.items():
        bind.execute(
            snapshots.insert().values(
                source_id=source_id,
                channel=channel,
                snapshot_date=snapshot_date,
                crawl_run_id=run["id"],
                is_baseline=snapshot_date == earliest[(source_id, channel)],
                created_at=now,
            )
        )


def _backfill_categories(
    bind: sa.Connection,
    metadata: sa.MetaData,
    now: datetime,
) -> None:
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    versions = sa.Table("job_versions", metadata, autoload_with=bind)
    categories = sa.Table("source_categories", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            versions.c.id.label("version_id"),
            versions.c.payload,
            jobs.c.source_id,
            jobs.c.category_id,
            jobs.c.category_name,
            jobs.c.category_parent_id,
            jobs.c.category_parent_name,
        ).join(jobs, jobs.c.id == versions.c.job_id)
    ).mappings()

    specs: dict[tuple[int, str], dict[str, Any]] = {}
    version_keys: dict[int, tuple[int, str]] = {}
    for row in rows:
        payload = _json_object(row["payload"])
        category_id = str(payload.get("category_id") or row["category_id"] or "")
        category_name = str(payload.get("category_name") or row["category_name"] or "Unknown")
        parent_id = str(payload.get("category_parent_id") or row["category_parent_id"] or "")
        parent_name = str(
            payload.get("category_parent_name") or row["category_parent_name"] or category_name
        )
        if not category_id:
            raise RuntimeError(f"Cannot backfill category for job version {row['version_id']}")
        key = (row["source_id"], category_id)
        parent_key = (
            (row["source_id"], parent_id)
            if parent_id and parent_id != category_id
            else None
        )
        specs[key] = {"name": category_name, "parent_key": parent_key}
        if parent_key is not None:
            specs.setdefault(parent_key, {"name": parent_name, "parent_key": None})
        version_keys[row["version_id"]] = key

    category_ids: dict[tuple[int, str], int] = {}
    for (source_id, external_id), spec in specs.items():
        category_ids[(source_id, external_id)] = bind.execute(
            categories.insert()
            .values(
                source_id=source_id,
                external_id=external_id,
                name=spec["name"],
                created_at=now,
                updated_at=now,
            )
            .returning(categories.c.id)
        ).scalar_one()
    for key, spec in specs.items():
        parent_key = spec["parent_key"]
        if parent_key is not None:
            bind.execute(
                categories.update()
                .where(categories.c.id == category_ids[key])
                .values(parent_id=category_ids[parent_key])
            )
    for version_id, key in version_keys.items():
        bind.execute(
            versions.update()
            .where(versions.c.id == version_id)
            .values(source_category_id=category_ids[key])
        )


def _backfill_first_canonical_seen(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    observations = sa.Table("job_observations", metadata, autoload_with=bind)
    snapshots = sa.Table("daily_snapshots", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            observations.c.job_id,
            sa.func.min(snapshots.c.snapshot_date).label("first_snapshot_date"),
        )
        .join(
            snapshots,
            snapshots.c.crawl_run_id == observations.c.crawl_run_id,
        )
        .group_by(observations.c.job_id)
    )
    for row in rows:
        bind.execute(
            jobs.update()
            .where(jobs.c.id == row.job_id)
            .values(first_canonical_seen_on=row.first_snapshot_date)
        )


def _backfill_version_locations(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    versions = sa.Table("job_versions", metadata, autoload_with=bind)
    locations = sa.Table("locations", metadata, autoload_with=bind)
    version_locations = sa.Table("job_version_locations", metadata, autoload_with=bind)
    location_ids = {
        (row.source_id, row.code): row.id
        for row in bind.execute(
            sa.select(locations.c.id, locations.c.source_id, locations.c.code)
        )
    }
    rows = bind.execute(
        sa.select(
            versions.c.id.label("version_id"),
            versions.c.payload,
            jobs.c.source_id,
        ).join(jobs, jobs.c.id == versions.c.job_id)
    ).mappings()
    for row in rows:
        payload = _json_object(row["payload"])
        payload_locations = payload.get("locations")
        if not isinstance(payload_locations, list) or not payload_locations:
            raise RuntimeError(f"Cannot backfill locations for job version {row['version_id']}")
        for item in payload_locations:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "")
            if not code:
                continue
            key = (row["source_id"], code)
            location_id = location_ids.get(key)
            if location_id is None:
                location_id = bind.execute(
                    locations.insert()
                    .values(
                        source_id=row["source_id"],
                        code=code,
                        name=str(item.get("name") or code),
                        country_code=item.get("country_code"),
                        country_name=item.get("country_name"),
                        state_code=item.get("state_code"),
                        state_name=item.get("state_name"),
                        district_code=item.get("district_code"),
                        district_name=item.get("district_name"),
                        address=item.get("address"),
                    )
                    .returning(locations.c.id)
                ).scalar_one()
                location_ids[key] = location_id
            bind.execute(
                version_locations.insert().values(
                    job_version_id=row["version_id"],
                    location_id=location_id,
                )
            )


def _backfill_observation_versions(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    versions = sa.Table("job_versions", metadata, autoload_with=bind)
    observations = sa.Table("job_observations", metadata, autoload_with=bind)
    versions_by_job: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(
        sa.select(versions.c.id, versions.c.job_id, versions.c.observed_at)
    ).mappings():
        versions_by_job[row["job_id"]].append(dict(row))
    for job_versions in versions_by_job.values():
        job_versions.sort(key=lambda item: (_as_aware(item["observed_at"]), item["id"]))

    for observation in bind.execute(sa.select(observations)).mappings():
        candidates = versions_by_job.get(observation["job_id"], [])
        if not candidates:
            raise RuntimeError(f"Observation {observation['id']} has no job version")
        observed_at = _as_aware(observation["observed_at"])
        applicable = [
            item for item in candidates if _as_aware(item["observed_at"]) <= observed_at
        ]
        version = applicable[-1] if applicable else candidates[0]
        bind.execute(
            observations.update()
            .where(observations.c.id == observation["id"])
            .values(job_version_id=version["id"])
        )


def _backfill_lifecycle(
    bind: sa.Connection,
    metadata: sa.MetaData,
) -> None:
    jobs = sa.Table("jobs", metadata, autoload_with=bind)
    versions = sa.Table("job_versions", metadata, autoload_with=bind)
    events = sa.Table("job_lifecycle_events", metadata, autoload_with=bind)
    versions_by_job: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(sa.select(versions)).mappings():
        versions_by_job[row["job_id"]].append(dict(row))
    for job_versions in versions_by_job.values():
        job_versions.sort(key=lambda item: (_as_aware(item["observed_at"]), item["id"]))

    for job in bind.execute(sa.select(jobs)).mappings():
        job_versions = versions_by_job.get(job["id"], [])
        if not job_versions:
            raise RuntimeError(f"Job {job['id']} has no version")
        first = job_versions[0]
        bind.execute(
            events.insert().values(
                job_id=job["id"],
                crawl_run_id=first["crawl_run_id"],
                job_version_id=first["id"],
                event_type="first_seen",
                effective_at=job["first_seen_at"],
                observed_at=first["observed_at"],
                details={"backfilled": True},
            )
        )
        for version in job_versions[1:]:
            bind.execute(
                events.insert().values(
                    job_id=job["id"],
                    crawl_run_id=version["crawl_run_id"],
                    job_version_id=version["id"],
                    event_type="changed",
                    effective_at=version["observed_at"],
                    observed_at=version["observed_at"],
                    details={"backfilled": True},
                )
            )
        if job["status"] == "closed" and job["closed_at"] is not None:
            bind.execute(
                jobs.update()
                .where(jobs.c.id == job["id"])
                .values(missing_since_at=job["closed_at"])
            )
            bind.execute(
                events.insert().values(
                    job_id=job["id"],
                    crawl_run_id=None,
                    job_version_id=job_versions[-1]["id"],
                    event_type="closed",
                    effective_at=job["closed_at"],
                    observed_at=job["closed_at"],
                    details={
                        "backfilled": True,
                        "precision": "legacy_detection_time",
                    },
                )
            )


def _backfill_canonical_locations(
    bind: sa.Connection,
    metadata: sa.MetaData,
    now: datetime,
) -> None:
    locations = sa.Table("locations", metadata, autoload_with=bind)
    canonical = sa.Table("canonical_locations", metadata, autoload_with=bind)
    mappings = sa.Table("source_location_mappings", metadata, autoload_with=bind)
    canonical_ids: dict[str, int] = {}
    for row in bind.execute(sa.select(locations)).mappings():
        data = dict(row)
        key = _canonical_location_key(data)
        canonical_id = canonical_ids.get(key)
        if canonical_id is None:
            canonical_id = bind.execute(
                canonical.insert()
                .values(
                    key=key,
                    level="city",
                    name=data["name"],
                    country_code=data["country_code"],
                    country_name=data["country_name"],
                    state_code=data["state_code"],
                    state_name=data["state_name"],
                    city_name=data["name"],
                    created_at=now,
                )
                .returning(canonical.c.id)
            ).scalar_one()
            canonical_ids[key] = canonical_id
        bind.execute(
            mappings.insert().values(
                location_id=data["id"],
                canonical_location_id=canonical_id,
                mapping_method="exact_source_fields",
                mapping_version="v1",
                is_current=True,
                confidence=1,
                created_at=now,
            )
        )


def _seed_topics(bind: sa.Connection, metadata: sa.MetaData, now: datetime) -> None:
    topics = sa.Table("topics", metadata, autoload_with=bind)
    aliases = sa.Table("topic_aliases", metadata, autoload_with=bind)
    topic_id = bind.execute(
        topics.insert()
        .values(
            taxonomy_version="v1",
            key="agent",
            name="Agent",
            description="AI agent and agentic-system related work",
            active=True,
            created_at=now,
        )
        .returning(topics.c.id)
    ).scalar_one()
    for alias in ("Agent", "AI Agent", "Agentic", "智能体"):
        bind.execute(
            aliases.insert().values(
                topic_id=topic_id,
                alias=alias,
                normalized_alias=alias.casefold(),
                match_mode="phrase",
            )
        )


def _finalize_constraints() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.alter_column("company_id", nullable=False)
        batch.alter_column("timezone", server_default=None)
        batch.create_foreign_key(
            "fk_sources_company_id",
            "companies",
            ["company_id"],
            ["id"],
        )
    with op.batch_alter_table("crawl_runs") as batch:
        batch.alter_column("snapshot_date", nullable=False)
    op.create_index(
        "ix_crawl_runs_source_channel_date",
        "crawl_runs",
        ["source_id", "channel", "snapshot_date"],
    )
    with op.batch_alter_table("job_versions") as batch:
        batch.alter_column("source_category_id", nullable=False)
        batch.create_foreign_key(
            "fk_job_versions_source_category_id",
            "source_categories",
            ["source_category_id"],
            ["id"],
        )
    op.create_index(
        "ix_job_versions_job_observed",
        "job_versions",
        ["job_id", "observed_at"],
    )
    with op.batch_alter_table("job_observations") as batch:
        batch.alter_column("job_version_id", nullable=False)
        batch.create_foreign_key(
            "fk_job_observations_job_version_id",
            "job_versions",
            ["job_version_id"],
            ["id"],
        )
    op.create_index(
        "ix_job_observations_run",
        "job_observations",
        ["crawl_run_id"],
    )
    op.create_index(
        "ix_job_observations_version",
        "job_observations",
        ["job_version_id"],
    )


def upgrade() -> None:
    _create_tables()
    bind = op.get_bind()
    metadata = sa.MetaData()
    now = _now()
    _backfill_companies(bind, metadata, now)
    metadata.clear()
    _backfill_runs_and_snapshots(bind, metadata, now)
    metadata.clear()
    _backfill_first_canonical_seen(bind, metadata)
    metadata.clear()
    _backfill_categories(bind, metadata, now)
    metadata.clear()
    _backfill_version_locations(bind, metadata)
    metadata.clear()
    _backfill_observation_versions(bind, metadata)
    metadata.clear()
    _backfill_lifecycle(bind, metadata)
    metadata.clear()
    _backfill_canonical_locations(bind, metadata, now)
    metadata.clear()
    _seed_topics(bind, metadata, now)
    _finalize_constraints()


def downgrade() -> None:
    op.drop_index("ix_job_observations_version", table_name="job_observations")
    op.drop_index("ix_job_observations_run", table_name="job_observations")
    with op.batch_alter_table("job_observations") as batch:
        batch.drop_constraint("fk_job_observations_job_version_id", type_="foreignkey")
        batch.drop_column("job_version_id")
    op.drop_index("ix_job_versions_job_observed", table_name="job_versions")
    with op.batch_alter_table("job_versions") as batch:
        batch.drop_constraint("fk_job_versions_source_category_id", type_="foreignkey")
        batch.drop_column("source_category_id")
    op.drop_index("ix_crawl_runs_source_channel_date", table_name="crawl_runs")
    with op.batch_alter_table("crawl_runs") as batch:
        batch.drop_column("snapshot_date")
    with op.batch_alter_table("sources") as batch:
        batch.drop_constraint("fk_sources_company_id", type_="foreignkey")
        batch.drop_column("timezone")
        batch.drop_column("company_id")

    op.drop_index("ix_job_derived_attributes_key", table_name="job_derived_attributes")
    op.drop_table("job_derived_attributes")
    op.drop_index(
        "ix_job_topic_mentions_topic_version",
        table_name="job_topic_mentions",
    )
    op.drop_table("job_topic_mentions")
    op.drop_table("topic_aliases")
    op.drop_table("topics")
    op.drop_index("ix_derivation_runs_current", table_name="derivation_runs")
    op.drop_table("derivation_runs")
    op.drop_table("source_location_mappings")
    op.drop_table("canonical_locations")
    op.drop_table("category_mappings")
    op.drop_table("canonical_categories")
    op.drop_index(
        "ix_job_lifecycle_events_type_observed",
        table_name="job_lifecycle_events",
    )
    op.drop_table("job_lifecycle_events")
    op.drop_table("job_version_locations")
    op.drop_index("ix_source_categories_parent", table_name="source_categories")
    op.drop_table("source_categories")
    op.drop_index("ix_daily_snapshots_date", table_name="daily_snapshots")
    op.drop_table("daily_snapshots")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("first_canonical_seen_on")
        batch.drop_column("missing_since_at")
    op.drop_table("companies")
