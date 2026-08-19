"""Initial source-fact and observation schema.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("partition_counts", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("external_code", sa.String(100), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("employment_type_id", sa.String(100), nullable=False),
        sa.Column("employment_type_name", sa.String(100), nullable=False),
        sa.Column("recruitment_project_id", sa.String(100)),
        sa.Column("recruitment_project_name", sa.String(300)),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category_id", sa.String(100), nullable=False),
        sa.Column("category_name", sa.String(300), nullable=False),
        sa.Column("category_parent_id", sa.String(100)),
        sa.Column("category_parent_name", sa.String(300)),
        sa.Column("is_hot", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("missing_streak", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
    )
    op.create_index(
        "ix_jobs_source_channel_status",
        "jobs",
        ["source_id", "channel", "status"],
    )
    op.create_table(
        "job_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("crawl_run_id", sa.String(36), sa.ForeignKey("crawl_runs.id"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "content_hash", name="uq_job_versions_hash"),
    )
    op.create_table(
        "job_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("crawl_run_id", sa.String(36), sa.ForeignKey("crawl_runs.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "crawl_run_id", name="uq_job_observation_run"),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("country_code", sa.String(100)),
        sa.Column("country_name", sa.String(300)),
        sa.Column("state_code", sa.String(100)),
        sa.Column("state_name", sa.String(300)),
        sa.Column("district_code", sa.String(100)),
        sa.Column("district_name", sa.String(300)),
        sa.Column("address", sa.Text()),
        sa.UniqueConstraint("source_id", "code", name="uq_locations_source_code"),
    )
    op.create_table(
        "job_locations",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id"),
            primary_key=True,
        ),
    )
    op.create_table(
        "raw_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("crawl_run_id", sa.String(36), sa.ForeignKey("crawl_runs.id"), nullable=False),
        sa.Column("path", sa.String(1500), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("partition", sa.String(500), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("raw_snapshots")
    op.drop_table("job_locations")
    op.drop_table("locations")
    op.drop_table("job_observations")
    op.drop_table("job_versions")
    op.drop_index("ix_jobs_source_channel_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("crawl_runs")
    op.drop_table("sources")
