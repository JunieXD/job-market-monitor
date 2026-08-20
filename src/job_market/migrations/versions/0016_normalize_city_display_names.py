"""Normalize Chinese city display names without changing source facts.

Revision ID: 0016
Revises: 0015

Source ``locations`` retain the exact labels returned by each recruitment
site. Automatic dimension mappings, snapshot mappings, and canonical display
names use a shared rule that removes the Chinese city suffix ``市`` so
``北京`` and ``北京市`` are analyzed as one city.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_METHODS = ("exact_source_fields", "normalized_city_name")


def _normalize_city_name(name: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKC", name).strip().casefold().split()
    )
    if (
        len(normalized) > 1
        and normalized.endswith("市")
        and any("\u4e00" <= char <= "\u9fff" for char in normalized)
    ):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        raise RuntimeError("Cannot migrate an empty city name")
    return normalized


def _canonical_key(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"city-name-{digest[:24]}"


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
    from importlib import import_module

    legacy = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    legacy._create_analysis_views()


def upgrade() -> None:
    connection = op.get_bind()
    _drop_analysis_views()
    metadata = sa.MetaData()
    locations = sa.Table("locations", metadata, autoload_with=connection)
    canonicals = sa.Table("canonical_locations", metadata, autoload_with=connection)
    mappings = sa.Table("source_location_mappings", metadata, autoload_with=connection)
    version_locations = sa.Table(
        "job_version_locations", metadata, autoload_with=connection
    )
    now = datetime.now(UTC)

    # Normalize canonical display labels, including manually published rows.
    # The source location table itself is deliberately never updated.
    for row in connection.execute(
        sa.select(canonicals.c.id, canonicals.c.name, canonicals.c.city_name).where(
            canonicals.c.level == "city"
        )
    ).mappings():
        normalized = _normalize_city_name(row["name"])
        values = {"name": normalized}
        if row["city_name"] is not None:
            values["city_name"] = _normalize_city_name(row["city_name"])
        connection.execute(
            canonicals.update().where(canonicals.c.id == row["id"]).values(**values)
        )

    source_rows = connection.execute(
        sa.select(
            locations.c.id.label("location_id"),
            locations.c.name,
            locations.c.country_code,
            locations.c.country_name,
            locations.c.state_code,
            locations.c.state_name,
        )
    ).mappings().all()

    for row in source_rows:
        current = connection.execute(
            sa.select(mappings).where(
                mappings.c.location_id == row["location_id"],
                mappings.c.is_current.is_(True),
            )
        ).mappings().one_or_none()
        if current is not None and current["mapping_method"] not in AUTO_METHODS:
            continue

        normalized = _normalize_city_name(row["name"])
        key = _canonical_key(normalized)
        canonical = connection.execute(
            sa.select(canonicals).where(canonicals.c.key == key)
        ).mappings().one_or_none()
        if canonical is None:
            canonical_id = connection.execute(
                canonicals.insert()
                .values(
                    key=key,
                    level="city",
                    name=normalized,
                    country_code=row["country_code"],
                    country_name=row["country_name"],
                    state_code=row["state_code"],
                    state_name=row["state_name"],
                    city_name=normalized,
                    created_at=now,
                )
                .returning(canonicals.c.id)
            ).scalar_one()
        else:
            canonical_id = canonical["id"]
            enrich = {
                field: row[field]
                for field in ("country_code", "country_name", "state_code", "state_name")
                if canonical[field] is None and row[field]
            }
            enrich.update({"name": normalized, "city_name": normalized})
            connection.execute(
                canonicals.update()
                .where(canonicals.c.id == canonical_id)
                .values(**enrich)
            )

        connection.execute(
            mappings.update()
            .where(
                mappings.c.location_id == row["location_id"],
                mappings.c.is_current.is_(True),
            )
            .values(is_current=False)
        )
        mapping_version = f"auto-city-name-v3-{key}"
        existing = connection.execute(
            sa.select(mappings).where(
                mappings.c.location_id == row["location_id"],
                mappings.c.mapping_version == mapping_version,
            )
        ).mappings().one_or_none()
        if existing is None:
            connection.execute(
                mappings.insert().values(
                    location_id=row["location_id"],
                    canonical_location_id=canonical_id,
                    mapping_method="normalized_city_name",
                    mapping_version=mapping_version,
                    is_current=True,
                    confidence=Decimal("0.9900"),
                    created_at=now,
                )
            )
        elif existing["canonical_location_id"] != canonical_id:
            raise RuntimeError("Existing city mapping points to a different canonical city")
        else:
            connection.execute(
                mappings.update()
                .where(mappings.c.id == existing["id"])
                .values(is_current=True)
            )

        # Re-point historical automatic snapshots so trend analysis also
        # merges the old 北京/北京市 split. Manual/model mappings remain intact.
        connection.execute(
            version_locations.update()
            .where(version_locations.c.location_id == row["location_id"])
            .where(version_locations.c.mapping_method.in_(AUTO_METHODS))
            .values(
                canonical_location_id=canonical_id,
                mapping_method="normalized_city_name",
                mapping_version=mapping_version,
                mapping_confidence=Decimal("0.9900"),
            )
        )

    _restore_analysis_views()


def downgrade() -> None:
    # The source facts and prior mappings remain available for audit. Reverting
    # the display normalization would reintroduce a known analytical split.
    pass
