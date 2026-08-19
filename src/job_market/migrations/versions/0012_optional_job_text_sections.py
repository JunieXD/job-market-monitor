"""Allow missing source-provided job text sections.

Revision ID: 0012
Revises: 0011

Some official portals publish only a combined body or omit either duties or
requirements. Missing source facts stay NULL instead of being inferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
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
        batch.alter_column(
            "description",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch.alter_column(
            "requirements",
            existing_type=sa.Text(),
            nullable=True,
        )
    _create_analysis_views()


def downgrade() -> None:
    _drop_analysis_views()
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column(
            "requirements",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.alter_column(
            "description",
            existing_type=sa.Text(),
            nullable=False,
        )
    _create_analysis_views()
