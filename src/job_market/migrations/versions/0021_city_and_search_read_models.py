"""Persist city aggregates and searchable character signatures.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_market.china_cities import china_city_name_sql, china_city_values_sql

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _character_codes(value: str | None) -> list[int]:
    return sorted(
        {
            ord(character)
            for character in (value or "").lower()
            if not character.isspace() or character in "\u00a0\u202f"
        }
    )


def _backfill_search_documents(connection: sa.Connection) -> None:
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                INSERT INTO job_search_documents (
                    job_id,
                    title_characters,
                    description_characters,
                    requirements_characters
                )
                SELECT id,
                       ARRAY(
                           SELECT DISTINCT ascii(character)
                           FROM regexp_split_to_table(
                               lower(COALESCE(title, '')), ''
                           ) AS character
                           WHERE character <> '' AND character !~ '^\\s$'
                           ORDER BY ascii(character)
                       ),
                       ARRAY(
                           SELECT DISTINCT ascii(character)
                           FROM regexp_split_to_table(
                               lower(COALESCE(description, '')), ''
                           ) AS character
                           WHERE character <> '' AND character !~ '^\\s$'
                           ORDER BY ascii(character)
                       ),
                       ARRAY(
                           SELECT DISTINCT ascii(character)
                           FROM regexp_split_to_table(
                               lower(COALESCE(requirements, '')), ''
                           ) AS character
                           WHERE character <> '' AND character !~ '^\\s$'
                           ORDER BY ascii(character)
                       )
                FROM jobs
                """
            )
        )
        return

    metadata = sa.MetaData()
    jobs = sa.Table("jobs", metadata, autoload_with=connection)
    documents = sa.Table("job_search_documents", metadata, autoload_with=connection)
    rows = connection.execute(
        sa.select(
            jobs.c.id,
            jobs.c.title,
            jobs.c.description,
            jobs.c.requirements,
        ).order_by(jobs.c.id)
    )
    while batch := rows.fetchmany(500):
        connection.execute(
            documents.insert(),
            [
                {
                    "job_id": row.id,
                    "title_characters": _character_codes(row.title),
                    "description_characters": _character_codes(row.description),
                    "requirements_characters": _character_codes(row.requirements),
                }
                for row in batch
            ],
        )


def _backfill_city_stats(connection: sa.Connection) -> None:
    standard_city_name = china_city_name_sql("cl.name")
    connection.execute(
        sa.text(
            f"""
            INSERT INTO daily_snapshot_city_stats (
                daily_snapshot_id,
                city_name,
                posting_count,
                fractional_posting_count
            )
            WITH china_city_links AS (
                SELECT DISTINCT jvlc.job_version_id,
                       {standard_city_name} AS city_name
                FROM job_version_location_cities AS jvlc
                JOIN canonical_locations AS cl
                  ON cl.id = jvlc.canonical_location_id
                WHERE {standard_city_name} IN ({china_city_values_sql()})
            ), version_location_counts AS (
                SELECT job_version_id, COUNT(*) AS location_count
                FROM china_city_links
                GROUP BY job_version_id
            )
            SELECT ds.id,
                   city.city_name,
                   COUNT(DISTINCT jo.job_id),
                   SUM(1.0 / vlc.location_count)
            FROM daily_snapshots AS ds
            JOIN job_observations AS jo
              ON jo.crawl_run_id = ds.crawl_run_id
            JOIN china_city_links AS city
              ON city.job_version_id = jo.job_version_id
            JOIN version_location_counts AS vlc
              ON vlc.job_version_id = jo.job_version_id
            GROUP BY ds.id, city.city_name
            """
        )
    )


def upgrade() -> None:
    character_codes_type = sa.ARRAY(sa.Integer()).with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "daily_snapshot_city_stats",
        sa.Column("daily_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("city_name", sa.String(length=100), nullable=False),
        sa.Column("posting_count", sa.Integer(), nullable=False),
        sa.Column(
            "fractional_posting_count",
            sa.Numeric(precision=30, scale=16),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["daily_snapshot_id"], ["daily_snapshots.id"]),
        sa.PrimaryKeyConstraint("daily_snapshot_id", "city_name"),
    )
    op.create_index(
        "ix_daily_snapshot_city_stats_city",
        "daily_snapshot_city_stats",
        ["city_name", "daily_snapshot_id"],
    )
    op.create_table(
        "job_search_documents",
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("title_characters", character_codes_type, nullable=False),
        sa.Column("description_characters", character_codes_type, nullable=False),
        sa.Column("requirements_characters", character_codes_type, nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("job_id"),
    )

    connection = op.get_bind()
    _backfill_search_documents(connection)
    for column_name in (
        "title_characters",
        "description_characters",
        "requirements_characters",
    ):
        op.create_index(
            f"ix_job_search_documents_{column_name}",
            "job_search_documents",
            [column_name],
            postgresql_using="gin",
        )
    _backfill_city_stats(connection)


def downgrade() -> None:
    op.drop_table("job_search_documents")
    op.drop_index(
        "ix_daily_snapshot_city_stats_city",
        table_name="daily_snapshot_city_stats",
    )
    op.drop_table("daily_snapshot_city_stats")
