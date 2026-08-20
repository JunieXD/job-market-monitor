"""Remove automatic city links created from placeholder-only labels.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from job_market.normalization import normalize_city_names

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_METHODS = ("exact_source_fields", "normalized_city_name")


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    locations = sa.Table("locations", metadata, autoload_with=connection)
    mappings = sa.Table("source_location_mappings", metadata, autoload_with=connection)
    version_locations = sa.Table(
        "job_version_locations", metadata, autoload_with=connection
    )
    city_history = sa.Table(
        "job_version_location_cities", metadata, autoload_with=connection
    )

    for location in connection.execute(sa.select(locations)).mappings():
        if normalize_city_names(location["name"]):
            continue

        connection.execute(
            mappings.update()
            .where(mappings.c.location_id == location["id"])
            .where(mappings.c.mapping_method.in_(AUTO_METHODS))
            .values(is_current=False)
        )
        connection.execute(
            city_history.delete()
            .where(city_history.c.location_id == location["id"])
            .where(city_history.c.mapping_method.in_(AUTO_METHODS))
        )
        connection.execute(
            version_locations.update()
            .where(version_locations.c.location_id == location["id"])
            .where(version_locations.c.mapping_method.in_(AUTO_METHODS))
            .values(
                canonical_location_id=None,
                mapping_method=None,
                mapping_version=None,
                mapping_confidence=None,
            )
        )


def downgrade() -> None:
    # The removed links were derived from invalid placeholder labels and are
    # intentionally not recreated.
    pass
