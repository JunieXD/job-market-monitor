"""Model source coverage, direct facts, category assignments, and field profiles.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_analysis_views() -> None:
    op.execute("DROP VIEW IF EXISTS daily_market_city_stats")
    op.execute("DROP VIEW IF EXISTS daily_market_category_stats")
    op.execute("DROP VIEW IF EXISTS daily_city_stats")
    op.execute("DROP VIEW IF EXISTS daily_category_stats")
    op.execute("DROP VIEW IF EXISTS daily_company_stats")


def _create_analysis_views() -> None:
    analytics = import_module("job_market.migrations.versions.0003_analytics_views")
    op.execute(analytics.COMPANY_VIEW)
    op.execute(CATEGORY_VIEW)
    op.execute(analytics.CITY_VIEW)
    op.execute(MARKET_CATEGORY_VIEW)
    op.execute(analytics.MARKET_CITY_VIEW)


CATEGORY_VIEW = """
CREATE VIEW daily_category_stats AS
WITH total_counts AS (
    SELECT
        ds.snapshot_date,
        ds.source_id,
        ds.channel,
        COUNT(DISTINCT jo.job_id) AS total_posting_count
    FROM daily_snapshots AS ds
    LEFT JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    GROUP BY ds.snapshot_date, ds.source_id, ds.channel
),
category_counts AS (
    SELECT
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key AS company_key,
        c.name AS company_name,
        ds.channel,
        sc.id AS source_category_id,
        COALESCE(sc.external_id, '__unclassified__') AS source_category_external_id,
        COALESCE(sc.name, '未分类') AS source_category_name,
        parent.name AS source_parent_category_name,
        jvsc.assignment_method AS category_assignment_method,
        cc.id AS canonical_category_id,
        cc.key AS canonical_category_key,
        cc.name AS canonical_category_name,
        COUNT(DISTINCT jo.job_id) AS posting_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN companies AS c ON c.id = s.company_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    LEFT JOIN job_version_source_categories AS jvsc
        ON jvsc.job_version_id = jo.job_version_id
    LEFT JOIN source_categories AS sc ON sc.id = jvsc.source_category_id
    LEFT JOIN source_categories AS parent ON parent.id = sc.parent_id
    LEFT JOIN category_mappings AS cm
        ON cm.source_category_id = sc.id AND cm.is_current
    LEFT JOIN canonical_categories AS cc ON cc.id = cm.canonical_category_id
    GROUP BY
        ds.snapshot_date,
        ds.source_id,
        s.company_id,
        c.key,
        c.name,
        ds.channel,
        sc.id,
        sc.external_id,
        sc.name,
        parent.name,
        jvsc.assignment_method,
        cc.id,
        cc.key,
        cc.name
)
SELECT
    category_counts.*,
    1.0 * category_counts.posting_count
        / NULLIF(total_counts.total_posting_count, 0) AS source_category_share
FROM category_counts
JOIN total_counts
    ON total_counts.snapshot_date = category_counts.snapshot_date
    AND total_counts.source_id = category_counts.source_id
    AND total_counts.channel = category_counts.channel
"""


MARKET_CATEGORY_VIEW = """
CREATE VIEW daily_market_category_stats AS
WITH total_counts AS (
    SELECT
        ds.snapshot_date,
        ds.channel,
        COUNT(DISTINCT jo.job_id) AS total_posting_count
    FROM daily_snapshots AS ds
    LEFT JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    GROUP BY ds.snapshot_date, ds.channel
),
category_counts AS (
    SELECT
        ds.snapshot_date,
        ds.channel,
        cc.id AS canonical_category_id,
        CASE
            WHEN sc.id IS NULL THEN 'unclassified'
            WHEN cc.id IS NULL THEN 'unmapped'
            ELSE cc.key
        END AS canonical_category_key,
        CASE
            WHEN sc.id IS NULL THEN '未分类'
            WHEN cc.id IS NULL THEN '未映射'
            ELSE cc.name
        END AS canonical_category_name,
        COUNT(DISTINCT jo.job_id) AS posting_count,
        COUNT(DISTINCT s.company_id) AS covered_company_count
    FROM daily_snapshots AS ds
    JOIN sources AS s ON s.id = ds.source_id
    JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
    LEFT JOIN job_version_source_categories AS jvsc
        ON jvsc.job_version_id = jo.job_version_id
    LEFT JOIN source_categories AS sc ON sc.id = jvsc.source_category_id
    LEFT JOIN category_mappings AS cm
        ON cm.source_category_id = sc.id AND cm.is_current
    LEFT JOIN canonical_categories AS cc ON cc.id = cm.canonical_category_id
    GROUP BY
        ds.snapshot_date,
        ds.channel,
        cc.id,
        CASE
            WHEN sc.id IS NULL THEN 'unclassified'
            WHEN cc.id IS NULL THEN 'unmapped'
            ELSE cc.key
        END,
        CASE
            WHEN sc.id IS NULL THEN '未分类'
            WHEN cc.id IS NULL THEN '未映射'
            ELSE cc.name
        END
)
SELECT
    category_counts.*,
    1.0 * category_counts.posting_count
        / NULLIF(total_counts.total_posting_count, 0) AS market_category_share
FROM category_counts
JOIN total_counts
    ON total_counts.snapshot_date = category_counts.snapshot_date
    AND total_counts.channel = category_counts.channel
"""


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)

    # SQLite rebuilds tables for nullability and constraint changes. All views
    # that reference those tables must be removed until the rebuild completes.
    _drop_analysis_views()

    op.add_column("sources", sa.Column("display_name", sa.String(300)))
    op.add_column("sources", sa.Column("source_type", sa.String(100)))
    op.add_column("sources", sa.Column("scope_name", sa.String(300)))
    sources = sa.Table("sources", sa.MetaData(), autoload_with=connection)
    rows = connection.execute(sa.select(sources)).mappings().all()
    known_profiles = {
        "bytedance_cn": (
            "字节跳动中国招聘官网",
            "company_career_portal",
            "字节跳动",
        ),
        "alibaba_cn": (
            "阿里巴巴集团统一校园招聘",
            "group_campus_portal",
            "阿里巴巴集团",
        ),
    }
    for row in rows:
        display_name, source_type, scope_name = known_profiles.get(
            row["key"],
            (row["company_name"], "company_career_portal", row["company_name"]),
        )
        connection.execute(
            sources.update()
            .where(sources.c.id == row["id"])
            .values(
                display_name=display_name,
                source_type=source_type,
                scope_name=scope_name,
            )
        )
    with op.batch_alter_table("sources") as batch:
        batch.alter_column("display_name", nullable=False)
        batch.alter_column("source_type", nullable=False)
        batch.alter_column("scope_name", nullable=False)

    op.create_table(
        "source_channels",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), primary_key=True),
        sa.Column("channel", sa.String(30), primary_key=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("coverage_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_source_channels_status",
        ),
    )
    source_channels = sa.Table(
        "source_channels", sa.MetaData(), autoload_with=connection
    )
    known_channels = {
        "bytedance_cn": ("campus", "experienced"),
        "alibaba_cn": ("campus",),
    }
    existing_run_channels = connection.execute(
        sa.text("SELECT DISTINCT source_id, channel FROM crawl_runs")
    ).mappings()
    channel_rows = {(row["source_id"], row["channel"]) for row in existing_run_channels}
    for row in rows:
        channel_rows.update((row["id"], channel) for channel in known_channels.get(row["key"], ()))
    if channel_rows:
        connection.execute(
            source_channels.insert(),
            [
                {
                    "source_id": source_id,
                    "channel": channel,
                    "status": "active",
                    "coverage_note": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for source_id, channel in sorted(channel_rows)
            ],
        )

    op.add_column("jobs", sa.Column("degree_code", sa.String(100)))
    op.add_column("jobs", sa.Column("degree_name", sa.String(300)))
    op.add_column("jobs", sa.Column("experience_min_years", sa.Integer()))
    op.add_column("jobs", sa.Column("experience_max_years", sa.Integer()))
    op.add_column("jobs", sa.Column("graduation_start_at", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("graduation_end_at", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("department_code", sa.String(200)))
    op.add_column("jobs", sa.Column("department_name", sa.String(300)))
    op.add_column(
        "jobs",
        sa.Column("interview_location_names", sa.JSON()),
    )
    jobs = sa.Table("jobs", sa.MetaData(), autoload_with=connection)
    connection.execute(
        jobs.update()
        .where(jobs.c.interview_location_names.is_(None))
        .values(interview_location_names=[])
    )
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("category_id", existing_type=sa.String(100), nullable=True)
        batch.alter_column("category_name", existing_type=sa.String(300), nullable=True)
        batch.alter_column(
            "interview_location_names",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_jobs_experience_range",
            "experience_min_years IS NULL OR experience_max_years IS NULL "
            "OR experience_min_years <= experience_max_years",
        )
        batch.create_check_constraint(
            "ck_jobs_graduation_range",
            "graduation_start_at IS NULL OR graduation_end_at IS NULL "
            "OR graduation_start_at <= graduation_end_at",
        )
    with op.batch_alter_table("job_versions") as batch:
        batch.alter_column(
            "source_category_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    op.create_table(
        "job_version_source_categories",
        sa.Column(
            "job_version_id",
            sa.Integer(),
            sa.ForeignKey("job_versions.id"),
            primary_key=True,
        ),
        sa.Column(
            "source_category_id",
            sa.Integer(),
            sa.ForeignKey("source_categories.id"),
            primary_key=True,
        ),
        sa.Column("assignment_method", sa.String(30), nullable=False),
        sa.CheckConstraint(
            "assignment_method IN ('direct_field', 'filter_membership')",
            name="ck_job_version_source_categories_method",
        ),
    )
    op.create_index(
        "ix_job_version_source_categories_category",
        "job_version_source_categories",
        ["source_category_id", "job_version_id"],
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO job_version_source_categories (
                job_version_id, source_category_id, assignment_method
            )
            SELECT id, source_category_id, 'direct_field'
            FROM job_versions
            WHERE source_category_id IS NOT NULL
            """
        )
    )

    op.create_table(
        "crawl_run_field_stats",
        sa.Column(
            "crawl_run_id",
            sa.String(36),
            sa.ForeignKey("crawl_runs.id"),
            primary_key=True,
        ),
        sa.Column("field_path", sa.String(500), primary_key=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("present_count", sa.Integer(), nullable=False),
        sa.Column("non_null_count", sa.Integer(), nullable=False),
        sa.Column("non_empty_count", sa.Integer(), nullable=False),
        sa.Column("type_counts", sa.JSON(), nullable=False),
        sa.CheckConstraint("row_count >= 0", name="ck_field_stats_row_count"),
        sa.CheckConstraint(
            "present_count >= 0 AND present_count <= row_count",
            name="ck_field_stats_present_count",
        ),
        sa.CheckConstraint(
            "non_null_count >= 0 AND non_null_count <= present_count",
            name="ck_field_stats_non_null_count",
        ),
        sa.CheckConstraint(
            "non_empty_count >= 0 AND non_empty_count <= non_null_count",
            name="ck_field_stats_non_empty_count",
        ),
    )

    _create_analysis_views()


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0009 separates optional and multi-category facts; automatic downgrade "
        "would discard valid source data. Restore from a pre-migration backup instead."
    )
