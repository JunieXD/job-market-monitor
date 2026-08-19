"""Distinguish primary topic jobs from related mentions.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence
from importlib import import_module
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TOPIC_VIEW = """
CREATE VIEW daily_topic_stats AS
SELECT
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key AS company_key,
    c.name AS company_name,
    ds.channel,
    t.id AS topic_id,
    t.taxonomy_version,
    t.key AS topic_key,
    t.name AS topic_name,
    COUNT(DISTINCT jo.job_id) AS active_posting_count,
    COUNT(
        DISTINCT CASE WHEN jtm.relevance = 'primary' THEN jo.job_id ELSE NULL END
    ) AS primary_posting_count,
    COUNT(
        DISTINCT CASE WHEN jtm.relevance = 'related' THEN jo.job_id ELSE NULL END
    ) AS related_posting_count,
    CASE
        WHEN ds.is_baseline THEN 0
        ELSE COUNT(
            DISTINCT CASE
                WHEN j.first_canonical_seen_on = ds.snapshot_date THEN jo.job_id
                ELSE NULL
            END
        )
    END AS new_posting_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN companies AS c ON c.id = s.company_id
JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
JOIN jobs AS j ON j.id = jo.job_id
JOIN job_topic_mentions AS jtm ON jtm.job_version_id = jo.job_version_id
JOIN derivation_runs AS dr
    ON dr.id = jtm.derivation_run_id
    AND dr.status = 'success'
    AND dr.is_current
JOIN topics AS t ON t.id = jtm.topic_id
GROUP BY
    ds.snapshot_date,
    ds.source_id,
    s.company_id,
    c.key,
    c.name,
    ds.channel,
    ds.is_baseline,
    t.id,
    t.taxonomy_version,
    t.key,
    t.name
"""


MARKET_TOPIC_VIEW = """
CREATE VIEW daily_market_topic_stats AS
SELECT
    ds.snapshot_date,
    ds.channel,
    t.id AS topic_id,
    t.taxonomy_version,
    t.key AS topic_key,
    t.name AS topic_name,
    COUNT(DISTINCT jo.job_id) AS active_posting_count,
    COUNT(
        DISTINCT CASE WHEN jtm.relevance = 'primary' THEN jo.job_id ELSE NULL END
    ) AS primary_posting_count,
    COUNT(
        DISTINCT CASE WHEN jtm.relevance = 'related' THEN jo.job_id ELSE NULL END
    ) AS related_posting_count,
    COUNT(
        DISTINCT CASE
            WHEN NOT ds.is_baseline
                AND j.first_canonical_seen_on = ds.snapshot_date
            THEN jo.job_id
            ELSE NULL
        END
    ) AS new_posting_count,
    COUNT(DISTINCT s.company_id) AS covered_company_count
FROM daily_snapshots AS ds
JOIN sources AS s ON s.id = ds.source_id
JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
JOIN jobs AS j ON j.id = jo.job_id
JOIN job_topic_mentions AS jtm ON jtm.job_version_id = jo.job_version_id
JOIN derivation_runs AS dr
    ON dr.id = jtm.derivation_run_id
    AND dr.status = 'success'
    AND dr.is_current
JOIN topics AS t ON t.id = jtm.topic_id
GROUP BY
    ds.snapshot_date,
    ds.channel,
    t.id,
    t.taxonomy_version,
    t.key,
    t.name
"""


def _parse_fields(value: Any) -> list[str]:
    if isinstance(value, str):
        import json

        value = json.loads(value)
    return value if isinstance(value, list) else []


def upgrade() -> None:
    op.add_column(
        "job_topic_mentions",
        sa.Column("relevance", sa.String(30), nullable=True),
    )
    bind = op.get_bind()
    metadata = sa.MetaData()
    mentions = sa.Table("job_topic_mentions", metadata, autoload_with=bind)
    for row in bind.execute(
        sa.select(mentions.c.id, mentions.c.matched_fields)
    ).mappings():
        relevance = (
            "primary" if "title" in _parse_fields(row["matched_fields"]) else "related"
        )
        bind.execute(
            mentions.update()
            .where(mentions.c.id == row["id"])
            .values(relevance=relevance)
        )

    op.execute("DROP VIEW IF EXISTS daily_market_topic_stats")
    op.execute("DROP VIEW IF EXISTS daily_topic_stats")
    with op.batch_alter_table("job_topic_mentions") as batch:
        batch.alter_column("relevance", nullable=False)
        batch.create_check_constraint(
            "ck_topic_relevance",
            "relevance IN ('primary', 'related')",
        )
    op.execute(TOPIC_VIEW)
    op.execute(MARKET_TOPIC_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS daily_market_topic_stats")
    op.execute("DROP VIEW IF EXISTS daily_topic_stats")
    with op.batch_alter_table("job_topic_mentions") as batch:
        batch.drop_constraint("ck_topic_relevance", type_="check")
        batch.drop_column("relevance")
    previous = import_module(
        "job_market.migrations.versions.0003_analytics_views"
    )
    op.execute(previous.TOPIC_VIEW)
    op.execute(previous.MARKET_TOPIC_VIEW)
