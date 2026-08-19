"""Store source-provided recruitment counts.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_analysis_views() -> None:
    analytics = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    analytics._drop_analysis_views()


def _create_analysis_views() -> None:
    analytics = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    analytics._create_analysis_views()


def upgrade() -> None:
    _drop_analysis_views()
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("recruitment_count", sa.Integer()))
        batch.create_check_constraint(
            "ck_jobs_recruitment_count",
            "recruitment_count IS NULL OR recruitment_count >= 0",
        )
    _create_analysis_views()


def downgrade() -> None:
    _drop_analysis_views()
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint("ck_jobs_recruitment_count", type_="check")
        batch.drop_column("recruitment_count")
    _create_analysis_views()
