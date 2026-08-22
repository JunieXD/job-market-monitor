"""Record each external LLM call attempt and cache usage.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_version_derivations") as batch:
        batch.add_column(sa.Column("cached_prompt_tokens", sa.Integer(), nullable=True))

    op.create_table(
        "llm_call_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_version_derivation_id", sa.Integer(), nullable=False),
        sa.Column("job_version_id", sa.Integer(), nullable=False),
        sa.Column("derivation_profile_id", sa.String(length=64), nullable=False),
        sa.Column("derivation_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=30), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=300), nullable=True),
        sa.Column("finish_reason", sa.String(length=100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_llm_call_logs_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_derivation_id"],
            ["job_version_derivations.id"],
        ),
        sa.ForeignKeyConstraint(["job_version_id"], ["job_versions.id"]),
        sa.ForeignKeyConstraint(
            ["derivation_profile_id"],
            ["derivation_profiles.id"],
        ),
        sa.ForeignKeyConstraint(["derivation_run_id"], ["derivation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_call_logs_run_started",
        "llm_call_logs",
        ["derivation_run_id", "started_at"],
    )
    op.create_index(
        "ix_llm_call_logs_job_started",
        "llm_call_logs",
        ["job_version_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_logs_job_started", table_name="llm_call_logs")
    op.drop_index("ix_llm_call_logs_run_started", table_name="llm_call_logs")
    op.drop_table("llm_call_logs")
    with op.batch_alter_table("job_version_derivations") as batch:
        batch.drop_column("cached_prompt_tokens")
