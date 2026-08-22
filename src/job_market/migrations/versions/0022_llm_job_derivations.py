"""Add versioned, incremental LLM job derivations.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "derivation_profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=30), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("taxonomy_sha256", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "name",
            "version",
            name="uq_derivation_profiles_version",
        ),
    )
    op.create_index(
        "uq_derivation_profiles_one_current",
        "derivation_profiles",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    with op.batch_alter_table("derivation_runs") as batch:
        batch.add_column(sa.Column("derivation_profile_id", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_derivation_runs_profile",
            "derivation_profiles",
            ["derivation_profile_id"],
            ["id"],
        )
    op.create_table(
        "job_version_derivations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_version_id", sa.Integer(), nullable=False),
        sa.Column("derivation_profile_id", sa.String(length=64), nullable=False),
        sa.Column("derivation_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=300), nullable=True),
        sa.Column("finish_reason", sa.String(length=100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_job_version_derivations_status",
        ),
        sa.ForeignKeyConstraint(
            ["derivation_profile_id"],
            ["derivation_profiles.id"],
        ),
        sa.ForeignKeyConstraint(["derivation_run_id"], ["derivation_runs.id"]),
        sa.ForeignKeyConstraint(["job_version_id"], ["job_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_version_id",
            "derivation_profile_id",
            name="uq_job_version_derivations_profile",
        ),
    )
    op.create_index(
        "ix_job_version_derivations_profile_status",
        "job_version_derivations",
        ["derivation_profile_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_version_derivations_profile_status",
        table_name="job_version_derivations",
    )
    op.drop_table("job_version_derivations")
    with op.batch_alter_table("derivation_runs") as batch:
        batch.drop_constraint("fk_derivation_runs_profile", type_="foreignkey")
        batch.drop_column("derivation_profile_id")
    op.drop_index(
        "uq_derivation_profiles_one_current",
        table_name="derivation_profiles",
    )
    op.drop_table("derivation_profiles")
