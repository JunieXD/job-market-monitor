"""Snapshot dimension mappings on observed job versions.

Revision ID: 0014
Revises: 0013

The current mapping for a source category or location may change after a job
version was observed. Persist the mapping used at observation time so later
analytics can remain stable.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_analysis_views() -> None:
    for view in (
        "daily_market_city_stats",
        "daily_market_category_stats",
        "daily_city_stats",
        "daily_category_stats",
        "daily_company_stats",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")


def _restore_analysis_views() -> None:
    legacy = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    legacy._create_analysis_views()


def _backfill_category_mappings() -> None:
    op.execute(
        sa.text(
            """
            UPDATE job_version_source_categories
            SET canonical_category_id = (
                    SELECT cm.canonical_category_id
                    FROM category_mappings AS cm
                    WHERE cm.source_category_id = job_version_source_categories.source_category_id
                      AND cm.is_current
                ),
                mapping_method = (
                    SELECT cm.mapping_method
                    FROM category_mappings AS cm
                    WHERE cm.source_category_id = job_version_source_categories.source_category_id
                      AND cm.is_current
                ),
                mapping_version = (
                    SELECT cm.mapping_version
                    FROM category_mappings AS cm
                    WHERE cm.source_category_id = job_version_source_categories.source_category_id
                      AND cm.is_current
                ),
                mapping_confidence = (
                    SELECT cm.confidence
                    FROM category_mappings AS cm
                    WHERE cm.source_category_id = job_version_source_categories.source_category_id
                      AND cm.is_current
                )
            WHERE EXISTS (
                SELECT 1
                FROM category_mappings AS cm
                WHERE cm.source_category_id = job_version_source_categories.source_category_id
                  AND cm.is_current
            )
            """
        )
    )


def _backfill_location_mappings() -> None:
    op.execute(
        sa.text(
            """
            UPDATE job_version_locations
            SET canonical_location_id = (
                    SELECT slm.canonical_location_id
                    FROM source_location_mappings AS slm
                    WHERE slm.location_id = job_version_locations.location_id
                      AND slm.is_current
                ),
                mapping_method = (
                    SELECT slm.mapping_method
                    FROM source_location_mappings AS slm
                    WHERE slm.location_id = job_version_locations.location_id
                      AND slm.is_current
                ),
                mapping_version = (
                    SELECT slm.mapping_version
                    FROM source_location_mappings AS slm
                    WHERE slm.location_id = job_version_locations.location_id
                      AND slm.is_current
                ),
                mapping_confidence = (
                    SELECT slm.confidence
                    FROM source_location_mappings AS slm
                    WHERE slm.location_id = job_version_locations.location_id
                      AND slm.is_current
                )
            WHERE EXISTS (
                SELECT 1
                FROM source_location_mappings AS slm
                WHERE slm.location_id = job_version_locations.location_id
                  AND slm.is_current
            )
            """
        )
    )


def upgrade() -> None:
    _drop_analysis_views()
    with op.batch_alter_table("job_version_source_categories") as batch:
        batch.add_column(sa.Column("canonical_category_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("mapping_method", sa.String(30), nullable=True))
        batch.add_column(sa.Column("mapping_version", sa.String(100), nullable=True))
        batch.add_column(sa.Column("mapping_confidence", sa.Numeric(5, 4), nullable=True))
        batch.create_foreign_key(
            "fk_jvsc_canonical_category_id",
            "canonical_categories",
            ["canonical_category_id"],
            ["id"],
        )
    with op.batch_alter_table("job_version_locations") as batch:
        batch.add_column(sa.Column("canonical_location_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("mapping_method", sa.String(30), nullable=True))
        batch.add_column(sa.Column("mapping_version", sa.String(100), nullable=True))
        batch.add_column(sa.Column("mapping_confidence", sa.Numeric(5, 4), nullable=True))
        batch.create_foreign_key(
            "fk_jvl_canonical_location_id",
            "canonical_locations",
            ["canonical_location_id"],
            ["id"],
        )
    op.create_index(
        "ix_job_version_source_categories_canonical",
        "job_version_source_categories",
        ["canonical_category_id"],
    )
    op.create_index(
        "ix_job_version_locations_canonical",
        "job_version_locations",
        ["canonical_location_id"],
    )
    _backfill_category_mappings()
    _backfill_location_mappings()
    _restore_analysis_views()


def downgrade() -> None:
    _drop_analysis_views()
    op.drop_index(
        "ix_job_version_locations_canonical",
        table_name="job_version_locations",
    )
    op.drop_index(
        "ix_job_version_source_categories_canonical",
        table_name="job_version_source_categories",
    )
    with op.batch_alter_table("job_version_locations") as batch:
        batch.drop_constraint("fk_jvl_canonical_location_id", type_="foreignkey")
        batch.drop_column("mapping_confidence")
        batch.drop_column("mapping_version")
        batch.drop_column("mapping_method")
        batch.drop_column("canonical_location_id")
    with op.batch_alter_table("job_version_source_categories") as batch:
        batch.drop_constraint("fk_jvsc_canonical_category_id", type_="foreignkey")
        batch.drop_column("mapping_confidence")
        batch.drop_column("mapping_version")
        batch.drop_column("mapping_method")
        batch.drop_column("canonical_category_id")
    _restore_analysis_views()
