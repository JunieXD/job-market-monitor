"""Represent every city in a structured source location label.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module

import sqlalchemy as sa
from alembic import op

from job_market.normalization import canonical_city_key, normalize_city_names

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_METHODS = ("exact_source_fields", "normalized_city_name")


def _drop_city_views() -> None:
    op.execute("DROP VIEW IF EXISTS daily_market_city_stats")
    op.execute("DROP VIEW IF EXISTS daily_city_stats")


def _drop_analysis_views() -> None:
    for view in (
        "daily_market_city_stats",
        "daily_market_category_stats",
        "daily_city_stats",
        "daily_category_stats",
        "daily_company_stats",
    ):
        op.execute(f"DROP VIEW IF EXISTS {view}")


def _create_city_views() -> None:
    op.execute(
        """
        CREATE VIEW daily_city_stats AS
        WITH version_location_counts AS (
            SELECT job_version_id,
                   COUNT(DISTINCT canonical_location_id) AS location_count
            FROM job_version_location_cities
            GROUP BY job_version_id
        ), city_counts AS (
            SELECT ds.snapshot_date, ds.source_id,
                   s.company_id, c.key AS company_key, c.name AS company_name,
                   ds.channel, cl.id AS canonical_location_id,
                   cl.key AS canonical_location_key, cl.name AS city_name,
                   cl.country_name, cl.state_name,
                   COUNT(DISTINCT jo.job_id) AS posting_count,
                   SUM(1.0 / vlc.location_count) AS fractional_posting_count
            FROM daily_snapshots AS ds
            JOIN sources AS s ON s.id = ds.source_id
            JOIN companies AS c ON c.id = s.company_id
            JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
            JOIN job_version_location_cities AS jvlc
              ON jvlc.job_version_id = jo.job_version_id
            JOIN version_location_counts AS vlc
              ON vlc.job_version_id = jo.job_version_id
            JOIN canonical_locations AS cl
              ON cl.id = jvlc.canonical_location_id
            GROUP BY ds.snapshot_date, ds.source_id, s.company_id,
                     c.key, c.name, ds.channel, cl.id, cl.key, cl.name,
                     cl.country_name, cl.state_name
        )
        SELECT city_counts.*,
               fractional_posting_count / NULLIF(
                   SUM(fractional_posting_count) OVER (
                       PARTITION BY snapshot_date, source_id, channel
                   ), 0
               ) AS fractional_share
        FROM city_counts
        """
    )
    op.execute(
        """
        CREATE VIEW daily_market_city_stats AS
        WITH version_location_counts AS (
            SELECT job_version_id,
                   COUNT(DISTINCT canonical_location_id) AS location_count
            FROM job_version_location_cities
            GROUP BY job_version_id
        ), city_counts AS (
            SELECT ds.snapshot_date, ds.channel,
                   cl.id AS canonical_location_id,
                   cl.key AS canonical_location_key, cl.name AS city_name,
                   cl.country_name, cl.state_name,
                   COUNT(DISTINCT jo.job_id) AS posting_count,
                   SUM(1.0 / vlc.location_count) AS fractional_posting_count,
                   COUNT(DISTINCT s.company_id) AS covered_company_count
            FROM daily_snapshots AS ds
            JOIN sources AS s ON s.id = ds.source_id
            JOIN job_observations AS jo ON jo.crawl_run_id = ds.crawl_run_id
            JOIN job_version_location_cities AS jvlc
              ON jvlc.job_version_id = jo.job_version_id
            JOIN version_location_counts AS vlc
              ON vlc.job_version_id = jo.job_version_id
            JOIN canonical_locations AS cl
              ON cl.id = jvlc.canonical_location_id
            GROUP BY ds.snapshot_date, ds.channel, cl.id, cl.key, cl.name,
                     cl.country_name, cl.state_name
        )
        SELECT city_counts.*,
               fractional_posting_count / NULLIF(
                   SUM(fractional_posting_count) OVER (
                       PARTITION BY snapshot_date, channel
                   ), 0
               ) AS fractional_share
        FROM city_counts
        """
    )


def _restore_analysis_views() -> None:
    _drop_analysis_views()
    legacy = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    legacy._create_analysis_views()
    _drop_city_views()
    _create_city_views()


def _ensure_canonical(
    connection: sa.Connection,
    canonicals: sa.Table,
    city_name: str,
    location: sa.RowMapping,
    now: datetime,
) -> int:
    key = canonical_city_key(city_name)
    canonical = connection.execute(
        sa.select(canonicals).where(canonicals.c.key == key)
    ).mappings().one_or_none()
    if canonical is None:
        return connection.execute(
            canonicals.insert()
            .values(
                key=key,
                level="city",
                name=city_name,
                country_code=location["country_code"],
                country_name=location["country_name"],
                state_code=location["state_code"],
                state_name=location["state_name"],
                city_name=city_name,
                created_at=now,
            )
            .returning(canonicals.c.id)
        ).scalar_one()
    enrich = {
        field: location[field]
        for field in ("country_code", "country_name", "state_code", "state_name")
        if canonical[field] is None and location[field]
    }
    values = {"name": city_name, "city_name": city_name, **enrich}
    connection.execute(
        canonicals.update().where(canonicals.c.id == canonical["id"]).values(**values)
    )
    return canonical["id"]


def _insert_city_history(
    connection: sa.Connection,
    city_history: sa.Table,
    *,
    job_version_id: int,
    location_id: int,
    canonical_location_id: int,
    mapping_method: str,
    mapping_version: str,
    mapping_confidence: Decimal,
) -> None:
    exists = connection.execute(
        sa.select(city_history.c.job_version_id).where(
            city_history.c.job_version_id == job_version_id,
            city_history.c.location_id == location_id,
            city_history.c.canonical_location_id == canonical_location_id,
        )
    ).first()
    if exists is None:
        connection.execute(
            city_history.insert().values(
                job_version_id=job_version_id,
                location_id=location_id,
                canonical_location_id=canonical_location_id,
                mapping_method=mapping_method,
                mapping_version=mapping_version,
                mapping_confidence=mapping_confidence,
            )
        )


def upgrade() -> None:
    connection = op.get_bind()
    _drop_city_views()
    op.create_table(
        "job_version_location_cities",
        sa.Column("job_version_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("canonical_location_id", sa.Integer(), nullable=False),
        sa.Column("mapping_method", sa.String(length=30), nullable=False),
        sa.Column("mapping_version", sa.String(length=100), nullable=False),
        sa.Column("mapping_confidence", sa.Numeric(5, 4), nullable=False),
        sa.ForeignKeyConstraint(["canonical_location_id"], ["canonical_locations.id"]),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        sa.ForeignKeyConstraint(["job_version_id"], ["job_versions.id"]),
        sa.PrimaryKeyConstraint(
            "job_version_id", "location_id", "canonical_location_id"
        ),
    )
    op.create_index(
        "ix_job_version_location_cities_canonical",
        "job_version_location_cities",
        ["canonical_location_id"],
    )
    op.drop_index(
        "uq_source_location_mappings_one_current",
        table_name="source_location_mappings",
    )

    metadata = sa.MetaData()
    locations = sa.Table("locations", metadata, autoload_with=connection)
    canonicals = sa.Table("canonical_locations", metadata, autoload_with=connection)
    mappings = sa.Table("source_location_mappings", metadata, autoload_with=connection)
    version_locations = sa.Table(
        "job_version_locations", metadata, autoload_with=connection
    )
    city_history = sa.Table(
        "job_version_location_cities", metadata, autoload_with=connection
    )
    now = datetime.now(UTC)

    location_rows = connection.execute(sa.select(locations)).mappings().all()
    for location in location_rows:
        location_id = location["id"]
        current_mappings = connection.execute(
            sa.select(mappings).where(
                mappings.c.location_id == location_id,
                mappings.c.is_current.is_(True),
            )
        ).mappings().all()
        manual = [row for row in current_mappings if row["mapping_method"] not in AUTO_METHODS]
        city_names = normalize_city_names(location["name"])
        current_auto = [row for row in current_mappings if row["mapping_method"] in AUTO_METHODS]
        for row in current_auto:
            connection.execute(
                mappings.update()
                .where(mappings.c.id == row["id"])
                .values(is_current=False)
            )

        canonical_ids: list[tuple[int, str, str, Decimal]] = []
        if manual:
            for row in manual:
                canonical_ids.append(
                    (
                        row["canonical_location_id"],
                        row["mapping_method"],
                        row["mapping_version"],
                        row["confidence"],
                    )
                )
        else:
            for city_name in city_names:
                canonical_id = _ensure_canonical(
                    connection, canonicals, city_name, location, now
                )
                key = canonical_city_key(city_name)
                mapping_version = f"auto-city-name-v5-{key}"
                existing = connection.execute(
                    sa.select(mappings).where(
                        mappings.c.location_id == location_id,
                        mappings.c.mapping_version == mapping_version,
                    )
                ).mappings().one_or_none()
                if existing is None:
                    connection.execute(
                        mappings.insert().values(
                            location_id=location_id,
                            canonical_location_id=canonical_id,
                            mapping_method="normalized_city_name",
                            mapping_version=mapping_version,
                            is_current=True,
                            confidence=Decimal("0.9900"),
                            created_at=now,
                        )
                    )
                else:
                    connection.execute(
                        mappings.update()
                        .where(mappings.c.id == existing["id"])
                        .values(
                            canonical_location_id=canonical_id,
                            is_current=True,
                            confidence=Decimal("0.9900"),
                        )
                    )
                canonical_ids.append(
                    (
                        canonical_id,
                        "normalized_city_name",
                        mapping_version,
                        Decimal("0.9900"),
                    )
                )

        history_rows = connection.execute(
            sa.select(version_locations).where(
                version_locations.c.location_id == location_id
            )
        ).mappings().all()
        for history in history_rows:
            if history["mapping_method"] not in AUTO_METHODS and history["canonical_location_id"]:
                _insert_city_history(
                    connection,
                    city_history,
                    job_version_id=history["job_version_id"],
                    location_id=location_id,
                    canonical_location_id=history["canonical_location_id"],
                    mapping_method=history["mapping_method"] or "legacy",
                    mapping_version=history["mapping_version"] or "legacy",
                    mapping_confidence=history["mapping_confidence"] or Decimal("1.0000"),
                )
                continue
            for canonical_id, method, version, confidence in canonical_ids:
                _insert_city_history(
                    connection,
                    city_history,
                    job_version_id=history["job_version_id"],
                    location_id=location_id,
                    canonical_location_id=canonical_id,
                    mapping_method=method,
                    mapping_version=version,
                    mapping_confidence=confidence,
                )
            if len(canonical_ids) == 1:
                canonical_id, method, version, confidence = canonical_ids[0]
                connection.execute(
                    version_locations.update()
                    .where(version_locations.c.job_version_id == history["job_version_id"])
                    .where(version_locations.c.location_id == location_id)
                    .values(
                        canonical_location_id=canonical_id,
                        mapping_method=method,
                        mapping_version=version,
                        mapping_confidence=confidence,
                    )
                )
            else:
                connection.execute(
                    version_locations.update()
                    .where(version_locations.c.job_version_id == history["job_version_id"])
                    .where(version_locations.c.location_id == location_id)
                    .values(
                        canonical_location_id=None,
                        mapping_method=None,
                        mapping_version=None,
                        mapping_confidence=None,
                    )
                )

    _restore_analysis_views()


def downgrade() -> None:
    # The association table and split mappings are retained for auditability.
    pass
