from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from job_market.models import (
    CanonicalCategory,
    CanonicalLocation,
    CategoryMapping,
    Location,
    SourceCategory,
    SourceLocationMapping,
)
from job_market.schemas import CategoryMappingRecord, LocationMappingRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DimensionRepository:
    def __init__(
        self,
        engine: Engine,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.engine = engine
        self.clock = clock

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Dimension clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def add_canonical_category(
        self,
        *,
        taxonomy_version: str,
        key: str,
        name: str,
        parent_key: str | None = None,
    ) -> int:
        with Session(self.engine) as session, session.begin():
            existing = session.scalar(
                select(CanonicalCategory).where(
                    CanonicalCategory.taxonomy_version == taxonomy_version,
                    CanonicalCategory.key == key,
                )
            )
            if existing is not None:
                if existing.name != name:
                    raise ValueError(
                        "Canonical category versions are immutable; create a new taxonomy version"
                    )
                return existing.id
            parent_id = None
            if parent_key is not None:
                parent = session.scalar(
                    select(CanonicalCategory).where(
                        CanonicalCategory.taxonomy_version == taxonomy_version,
                        CanonicalCategory.key == parent_key,
                    )
                )
                if parent is None:
                    raise ValueError(f"Unknown canonical parent category: {parent_key}")
                parent_id = parent.id
            category = CanonicalCategory(
                taxonomy_version=taxonomy_version,
                key=key,
                name=name,
                parent_id=parent_id,
                active=True,
                created_at=self._now(),
            )
            session.add(category)
            session.flush()
            return category.id

    def add_canonical_location(
        self,
        *,
        key: str,
        name: str,
        level: str = "city",
        country_code: str | None = None,
        country_name: str | None = None,
        state_code: str | None = None,
        state_name: str | None = None,
        city_name: str | None = None,
    ) -> int:
        with Session(self.engine) as session, session.begin():
            existing = session.scalar(
                select(CanonicalLocation).where(CanonicalLocation.key == key)
            )
            if existing is not None:
                if existing.name != name or existing.level != level:
                    raise ValueError(
                        "Canonical locations are immutable; use a new canonical key"
                    )
                return existing.id
            location = CanonicalLocation(
                key=key,
                level=level,
                name=name,
                country_code=country_code,
                country_name=country_name,
                state_code=state_code,
                state_name=state_name,
                city_name=city_name or (name if level == "city" else None),
                created_at=self._now(),
            )
            session.add(location)
            session.flush()
            return location.id

    def publish_category_mappings(
        self,
        records: Iterable[CategoryMappingRecord],
    ) -> int:
        mappings = list(records)
        if len({item.source_category_id for item in mappings}) != len(mappings):
            raise ValueError("A mapping publication contains duplicate source categories")
        with Session(self.engine) as session, session.begin():
            for record in mappings:
                source_category = session.get(
                    SourceCategory,
                    record.source_category_id,
                    with_for_update=True,
                )
                if source_category is None:
                    raise ValueError(
                        f"Unknown source category: {record.source_category_id}"
                    )
                canonical = session.scalar(
                    select(CanonicalCategory).where(
                        CanonicalCategory.taxonomy_version == record.taxonomy_version,
                        CanonicalCategory.key == record.canonical_category_key,
                    )
                )
                if canonical is None:
                    raise ValueError(
                        "Unknown canonical category: "
                        f"{record.taxonomy_version}/{record.canonical_category_key}"
                    )
                session.execute(
                    update(CategoryMapping)
                    .where(
                        CategoryMapping.source_category_id == source_category.id,
                        CategoryMapping.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                existing = session.scalar(
                    select(CategoryMapping).where(
                        CategoryMapping.source_category_id == source_category.id,
                        CategoryMapping.mapping_version == record.mapping_version,
                    )
                )
                if existing is not None:
                    if existing.canonical_category_id != canonical.id:
                        raise ValueError(
                            "Published mapping versions are immutable; use a new version"
                        )
                    existing.is_current = True
                else:
                    session.add(
                        CategoryMapping(
                            source_category_id=source_category.id,
                            canonical_category_id=canonical.id,
                            mapping_method=record.mapping_method,
                            mapping_version=record.mapping_version,
                            is_current=True,
                            confidence=Decimal(str(record.confidence)),
                            evidence=record.evidence,
                            created_at=self._now(),
                        )
                    )
        return len(mappings)

    def publish_location_mappings(
        self,
        records: Iterable[LocationMappingRecord],
    ) -> int:
        mappings = list(records)
        if len({item.location_id for item in mappings}) != len(mappings):
            raise ValueError("A mapping publication contains duplicate source locations")
        with Session(self.engine) as session, session.begin():
            for record in mappings:
                location = session.get(
                    Location,
                    record.location_id,
                    with_for_update=True,
                )
                if location is None:
                    raise ValueError(f"Unknown source location: {record.location_id}")
                canonical = session.scalar(
                    select(CanonicalLocation).where(
                        CanonicalLocation.key == record.canonical_location_key
                    )
                )
                if canonical is None:
                    raise ValueError(
                        f"Unknown canonical location: {record.canonical_location_key}"
                    )
                session.execute(
                    update(SourceLocationMapping)
                    .where(
                        SourceLocationMapping.location_id == location.id,
                        SourceLocationMapping.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                existing = session.scalar(
                    select(SourceLocationMapping).where(
                        SourceLocationMapping.location_id == location.id,
                        SourceLocationMapping.mapping_version == record.mapping_version,
                    )
                )
                if existing is not None:
                    if existing.canonical_location_id != canonical.id:
                        raise ValueError(
                            "Published mapping versions are immutable; use a new version"
                        )
                    existing.is_current = True
                else:
                    session.add(
                        SourceLocationMapping(
                            location_id=location.id,
                            canonical_location_id=canonical.id,
                            mapping_method=record.mapping_method,
                            mapping_version=record.mapping_version,
                            is_current=True,
                            confidence=Decimal(str(record.confidence)),
                            created_at=self._now(),
                        )
                    )
        return len(mappings)
