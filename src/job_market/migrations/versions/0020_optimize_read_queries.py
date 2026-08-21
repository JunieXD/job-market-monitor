"""Persist daily rollups and add read-path indexes.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COMPANY_VIEW = """
CREATE VIEW daily_company_stats AS
SELECT
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key AS company_key,
    c.name AS company_name,
    ds.channel,
    ds.is_baseline,
    ds.active_posting_count,
    ds.new_posting_count,
    ds.changed_posting_count,
    ds.first_missing_posting_count,
    ds.closed_posting_count,
    ds.reopened_posting_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN companies AS c ON c.id = s.company_id
"""


ROLLUP_COLUMNS = (
    "active_posting_count",
    "new_posting_count",
    "changed_posting_count",
    "first_missing_posting_count",
    "closed_posting_count",
    "reopened_posting_count",
)


def _backfill_rollups(connection: sa.Connection) -> None:
    snapshots = connection.execute(
        sa.text(
            """
            SELECT id, crawl_run_id, snapshot_date, is_baseline
            FROM daily_snapshots
            ORDER BY id
            """
        )
    ).mappings().all()
    for snapshot in snapshots:
        active_count = connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM job_observations
                WHERE crawl_run_id = :crawl_run_id
                """
            ),
            {"crawl_run_id": snapshot["crawl_run_id"]},
        ).scalar_one()
        new_count = 0
        if not snapshot["is_baseline"]:
            new_count = connection.execute(
                sa.text(
                    """
                    SELECT COUNT(*)
                    FROM job_observations AS jo
                    JOIN jobs AS j ON j.id = jo.job_id
                    WHERE jo.crawl_run_id = :crawl_run_id
                      AND j.first_canonical_seen_on = :snapshot_date
                    """
                ),
                {
                    "crawl_run_id": snapshot["crawl_run_id"],
                    "snapshot_date": snapshot["snapshot_date"],
                },
            ).scalar_one()
        event_counts = connection.execute(
            sa.text(
                """
                SELECT
                    SUM(CASE WHEN event_type = 'changed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN event_type = 'missing' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN event_type = 'closed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN event_type = 'reopened' THEN 1 ELSE 0 END)
                FROM job_lifecycle_events
                WHERE crawl_run_id = :crawl_run_id
                """
            ),
            {"crawl_run_id": snapshot["crawl_run_id"]},
        ).one()
        connection.execute(
            sa.text(
                """
                UPDATE daily_snapshots
                SET active_posting_count = :active_count,
                    new_posting_count = :new_count,
                    changed_posting_count = :changed_count,
                    first_missing_posting_count = :missing_count,
                    closed_posting_count = :closed_count,
                    reopened_posting_count = :reopened_count
                WHERE id = :snapshot_id
                """
            ),
            {
                "snapshot_id": snapshot["id"],
                "active_count": active_count,
                "new_count": new_count,
                "changed_count": event_counts[0] or 0,
                "missing_count": event_counts[1] or 0,
                "closed_count": event_counts[2] or 0,
                "reopened_count": event_counts[3] or 0,
            },
        )


def upgrade() -> None:
    for column_name in ROLLUP_COLUMNS:
        op.add_column(
            "daily_snapshots",
            sa.Column(
                column_name,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    op.create_index(
        "ix_jobs_status_last_seen_id",
        "jobs",
        ["status", "last_seen_at", "id"],
    )
    op.create_index(
        "ix_job_lifecycle_events_run_type",
        "job_lifecycle_events",
        ["crawl_run_id", "event_type"],
    )

    connection = op.get_bind()
    _backfill_rollups(connection)
    op.execute("DROP VIEW IF EXISTS daily_company_stats")
    op.execute(COMPANY_VIEW)


def downgrade() -> None:
    city_migration = import_module(
        "job_market.migrations.versions.0018_split_city_location_mappings"
    )
    city_migration._drop_analysis_views()
    op.drop_index(
        "ix_job_lifecycle_events_run_type",
        table_name="job_lifecycle_events",
    )
    op.drop_index("ix_jobs_status_last_seen_id", table_name="jobs")
    with op.batch_alter_table("daily_snapshots") as batch:
        for column_name in reversed(ROLLUP_COLUMNS):
            batch.drop_column(column_name)
    city_migration._restore_analysis_views()
