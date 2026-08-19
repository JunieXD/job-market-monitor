"""Allow source-specific optional facts and retain source update metadata.

Revision ID: 0006
Revises: 0005

Some official career portals do not expose a publication timestamp, display
code, or hot flag.  These values must remain NULL instead of being fabricated.
"""

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
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
    op.execute(analytics.CATEGORY_VIEW)
    op.execute(analytics.CITY_VIEW)
    op.execute(analytics.MARKET_CATEGORY_VIEW)
    op.execute(analytics.MARKET_CITY_VIEW)


def upgrade() -> None:
    # SQLite rebuilds the table for nullability changes. Views referencing
    # `jobs` must be temporarily removed or SQLite refuses the final rename.
    _drop_analysis_views()
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column(
            "external_code",
            existing_type=sa.String(100),
            nullable=True,
        )
        batch.alter_column(
            "published_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
        batch.alter_column(
            "is_hot",
            existing_type=sa.Boolean(),
            nullable=True,
        )
        batch.add_column(sa.Column("source_updated_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("source_status", sa.String(100)))
    _create_analysis_views()


def downgrade() -> None:
    _drop_analysis_views()
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("source_status")
        batch.drop_column("source_updated_at")
        batch.alter_column(
            "is_hot",
            existing_type=sa.Boolean(),
            nullable=False,
        )
        batch.alter_column(
            "published_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.alter_column(
            "external_code",
            existing_type=sa.String(100),
            nullable=False,
        )
    _create_analysis_views()
