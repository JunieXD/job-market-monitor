"""Normalize structured city labels without changing source location facts.

Revision ID: 0017
Revises: 0016

This migration extends the automatic city mapping rule to labels such as
``深圳总部`` and ``四川省·成都``. Existing automatic mappings and historical
job-version mappings are repointed to the new city dimension. Manual and model
mappings remain untouched.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_METHODS = ("exact_source_fields", "normalized_city_name")
LOCATION_PART_RE = re.compile(r"[·•・‧∙/／|｜,，、;；]+|[-－—]+")
REGION_PREFIX_RE = re.compile(
    r"^[\u4e00-\u9fff]{2,20}(?:省|自治区|特别行政区|自治州|地区|盟)"
    r"[\s·•・‧∙/／|｜,，、;；:：\-－—]*(?P<city>.+)$"
)
CITY_WITH_DISTRICT_RE = re.compile(
    r"^(?P<city>.+?市)(?:.+(?:区|县|旗|镇|街道))$"
)
REGION_SUFFIXES = (
    "特别行政区",
    "自治区",
    "自治州",
    "地区",
    "省",
    "盟",
)
WORKPLACE_SUFFIXES = (
    "经济技术开发区",
    "经济开发区",
    "工业园区",
    "科技园区",
    "产业园区",
    "研发中心",
    "技术中心",
    "运营中心",
    "分公司",
    "子公司",
    "办事处",
    "代表处",
    "总部",
    "高新区",
    "经开区",
    "开发区",
    "新区",
)
NON_CITY_REGIONS = frozenset(
    {
        "中国",
        "中国大陆",
        "中国内地",
        "全国",
        "全国各地",
        "境内",
        "海外",
        "全球",
        "华东",
        "华南",
        "华北",
        "华中",
        "西南",
        "西北",
        "东北",
    }
)


def _is_region_part(value: str) -> bool:
    return value in NON_CITY_REGIONS or value.endswith(REGION_SUFFIXES)


def _strip_workplace_suffixes(value: str) -> str:
    candidate = value
    changed = True
    while changed:
        changed = False
        for suffix in WORKPLACE_SUFFIXES:
            if candidate.endswith(suffix) and len(candidate) > len(suffix):
                candidate = candidate[: -len(suffix)].rstrip(
                    " ·•・‧∙/／|｜,，、;；:-－—"
                )
                changed = True
                break
    return candidate


def _city_part(value: str) -> str:
    parts = [part.strip() for part in LOCATION_PART_RE.split(value) if part.strip()]
    if len(parts) > 1:
        region_indexes = [
            index for index, part in enumerate(parts[:-1]) if _is_region_part(part)
        ]
        if region_indexes:
            tail = parts[region_indexes[-1] + 1 :]
            marked_city = [part for part in tail if part.endswith("市")]
            if len(marked_city) == 1:
                return marked_city[0]
            if len(tail) == 1 or len(set(tail)) == 1 or tail[-1].endswith(
                WORKPLACE_SUFFIXES
            ):
                return tail[0]
    match = REGION_PREFIX_RE.match(value)
    if match is not None:
        return match.group("city")
    return value


def _normalize_city_name(name: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKC", name).strip().casefold().split()
    )
    if not any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return normalized
    candidate = _strip_workplace_suffixes(_city_part(normalized))
    district_match = CITY_WITH_DISTRICT_RE.match(candidate)
    if district_match is not None:
        candidate = district_match.group("city")
    if candidate.endswith("特别行政区") and len(candidate) > len("特别行政区"):
        candidate = candidate[: -len("特别行政区")]
    if len(candidate) > 1 and candidate.endswith("市"):
        candidate = candidate[:-1]
    return candidate.rstrip(" ·•・‧∙/／|｜,，、;；:-－—")


def _is_city_level_name(name: str) -> bool:
    normalized = _normalize_city_name(name)
    return bool(normalized) and normalized not in NON_CITY_REGIONS and not _is_region_part(
        normalized
    )


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

    source_rows = connection.execute(
        sa.select(
            locations.c.id.label("location_id"),
            locations.c.name,
            locations.c.country_code,
            locations.c.country_name,
            locations.c.state_code,
            locations.c.state_name,
            mappings.c.id.label("mapping_id"),
        )
        .join(mappings, mappings.c.location_id == locations.c.id)
        .where(
            mappings.c.is_current.is_(True),
            mappings.c.mapping_method.in_(AUTO_METHODS),
        )
    ).mappings().all()

    for row in source_rows:
        normalized = _normalize_city_name(row["name"])
        if not _is_city_level_name(row["name"]):
            connection.execute(
                mappings.update()
                .where(mappings.c.id == row["mapping_id"])
                .values(is_current=False)
            )
            connection.execute(
                version_locations.update()
                .where(version_locations.c.location_id == row["location_id"])
                .where(version_locations.c.mapping_method.in_(AUTO_METHODS))
                .values(
                    canonical_location_id=None,
                    mapping_method=None,
                    mapping_version=None,
                    mapping_confidence=None,
                )
            )
            continue
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
            connection.execute(
                canonicals.update()
                .where(canonicals.c.id == canonical_id)
                .values(name=normalized, city_name=normalized)
            )
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
            .where(mappings.c.id == row["mapping_id"])
            .values(is_current=False)
        )
        mapping_version = f"auto-city-name-v4-{key}"
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
        else:
            connection.execute(
                mappings.update()
                .where(mappings.c.id == existing["id"])
                .values(canonical_location_id=canonical_id, is_current=True)
            )

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
    # Source facts and previous mappings remain available for audit.
    pass
