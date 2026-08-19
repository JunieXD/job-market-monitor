"""Unify automatic city mappings when source metadata differs.

Revision ID: 0007
Revises: 0006

Alibaba currently exposes city names without country/state fields while
ByteDance exposes those optional fields.  The old exact-field key therefore
split the same named city across companies.  This migration publishes a new
automatic mapping version based on normalized city name, while retaining the
old mappings and canonical rows for audit.
"""

import hashlib
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_METHODS = ("exact_source_fields", "normalized_city_name")


def _canonical_key(name: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", name).strip().casefold().split())
    if not normalized:
        raise RuntimeError("Cannot migrate an empty source city name")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"city-name-{digest[:24]}"


def _metadata_score(row: sa.RowMapping) -> int:
    return sum(
        bool(row[field])
        for field in ("country_code", "country_name", "state_code", "state_name")
    )


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    locations = sa.Table("locations", metadata, autoload_with=connection)
    canonicals = sa.Table("canonical_locations", metadata, autoload_with=connection)
    mappings = sa.Table("source_location_mappings", metadata, autoload_with=connection)

    rows = connection.execute(
        sa.select(
            locations.c.id.label("location_id"),
            locations.c.name,
            locations.c.country_code,
            locations.c.country_name,
            locations.c.state_code,
            locations.c.state_name,
        )
        .join(mappings, mappings.c.location_id == locations.c.id)
        .where(
            mappings.c.is_current.is_(True),
            mappings.c.mapping_method.in_(AUTO_METHODS),
        )
    ).mappings()
    now = datetime.now(UTC)

    # Richer source metadata wins when the first canonical row is created.
    for row in sorted(rows, key=_metadata_score, reverse=True):
        key = _canonical_key(row["name"])
        canonical = connection.execute(
            sa.select(canonicals).where(canonicals.c.key == key)
        ).mappings().one_or_none()
        if canonical is None:
            canonical_id = connection.execute(
                canonicals.insert()
                .values(
                    key=key,
                    level="city",
                    name=row["name"],
                    country_code=row["country_code"],
                    country_name=row["country_name"],
                    state_code=row["state_code"],
                    state_name=row["state_name"],
                    city_name=row["name"],
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
            if enrich:
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
        mapping_version = f"auto-city-name-v2-{key}"
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
            raise RuntimeError(
                "Existing automatic location mapping points to a different city"
            )
        else:
            connection.execute(
                mappings.update()
                .where(mappings.c.id == existing["id"])
                .values(is_current=True)
            )


def downgrade() -> None:
    # This is a non-destructive data correction. Old mappings and canonical
    # rows remain available for audit, but reactivating a known-bad split would
    # corrupt cross-company history, so the corrected publication is retained.
    pass
