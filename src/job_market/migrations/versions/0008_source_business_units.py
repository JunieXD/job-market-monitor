"""Persist source-provided business-unit facts per job version.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_business_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("external_code", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_source_business_units_code_history",
        "source_business_units",
        ["source_id", "external_code"],
    )
    op.create_index(
        "uq_source_business_units_one_current",
        "source_business_units",
        ["source_id", "external_code"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
        sqlite_where=sa.text("valid_to IS NULL"),
    )
    op.create_table(
        "job_version_business_units",
        sa.Column(
            "job_version_id",
            sa.Integer(),
            sa.ForeignKey("job_versions.id"),
            primary_key=True,
        ),
        sa.Column(
            "source_business_unit_id",
            sa.Integer(),
            sa.ForeignKey("source_business_units.id"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("job_version_business_units")
    op.drop_index(
        "uq_source_business_units_one_current",
        table_name="source_business_units",
    )
    op.drop_index(
        "ix_source_business_units_code_history",
        table_name="source_business_units",
    )
    op.drop_table("source_business_units")
