"""Separate source-fact contract enrichment from market content changes.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

V2 = "v2"
V3 = "v3"


def _drop_analysis_views() -> None:
    analytics = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    analytics._drop_analysis_views()


def _create_analysis_views() -> None:
    analytics = import_module(
        "job_market.migrations.versions.0009_source_contracts_and_category_assignments"
    )
    analytics._create_analysis_views()


def upgrade() -> None:
    connection = op.get_bind()
    _drop_analysis_views()

    op.add_column("job_versions", sa.Column("fact_contract_version", sa.String(30)))
    versions = sa.Table("job_versions", sa.MetaData(), autoload_with=connection)
    for row in connection.execute(
        sa.select(versions.c.id, versions.c.payload)
    ).mappings():
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        contract_version = V3 if "categories" in payload else V2
        connection.execute(
            versions.update()
            .where(versions.c.id == row["id"])
            .values(fact_contract_version=contract_version)
        )
    with op.batch_alter_table("job_versions") as batch:
        batch.alter_column("fact_contract_version", nullable=False)

    with op.batch_alter_table("job_lifecycle_events") as batch:
        batch.drop_constraint("ck_job_lifecycle_event_type", type_="check")
        batch.create_check_constraint(
            "ck_job_lifecycle_event_type",
            "event_type IN ('first_seen', 'changed', 'missing', 'recovered', "
            "'closed', 'reopened', 'enriched')",
        )

    versions = sa.Table("job_versions", sa.MetaData(), autoload_with=connection)
    events = sa.Table("job_lifecycle_events", sa.MetaData(), autoload_with=connection)
    jobs = sa.Table("jobs", sa.MetaData(), autoload_with=connection)
    enriched_job_ids: set[int] = set()
    changed_events = connection.execute(
        sa.select(
            events.c.id,
            events.c.job_id,
            events.c.job_version_id,
            events.c.details,
            versions.c.fact_contract_version,
        )
        .join(versions, versions.c.id == events.c.job_version_id)
        .where(events.c.event_type == "changed")
    ).mappings()
    for event in changed_events:
        if event["fact_contract_version"] != V3:
            continue
        previous_contract = connection.execute(
            sa.select(versions.c.fact_contract_version)
            .where(
                versions.c.job_id == event["job_id"],
                versions.c.id < event["job_version_id"],
            )
            .order_by(versions.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous_contract != V2:
            continue
        details = dict(event["details"] or {})
        details.update(
            {
                "previous_fact_contract_version": V2,
                "fact_contract_version": V3,
            }
        )
        connection.execute(
            events.update()
            .where(events.c.id == event["id"])
            .values(event_type="enriched", details=details)
        )
        enriched_job_ids.add(event["job_id"])

    # A pre-0010 run may have updated last_changed_at before the event was
    # reclassified. Restore that field to the latest actual market change.
    for job_id in enriched_job_ids:
        last_market_change = connection.execute(
            sa.select(sa.func.max(events.c.observed_at)).where(
                events.c.job_id == job_id,
                events.c.event_type == "changed",
            )
        ).scalar_one_or_none()
        if last_market_change is None:
            last_market_change = connection.execute(
                sa.select(sa.func.min(events.c.observed_at)).where(
                    events.c.job_id == job_id,
                    events.c.event_type == "first_seen",
                )
            ).scalar_one_or_none()
        if last_market_change is not None:
            connection.execute(
                jobs.update()
                .where(jobs.c.id == job_id)
                .values(last_changed_at=last_market_change)
            )

    _create_analysis_views()


def downgrade() -> None:
    connection = op.get_bind()
    _drop_analysis_views()

    events = sa.Table("job_lifecycle_events", sa.MetaData(), autoload_with=connection)
    connection.execute(
        events.update().where(events.c.event_type == "enriched").values(event_type="changed")
    )
    with op.batch_alter_table("job_lifecycle_events") as batch:
        batch.drop_constraint("ck_job_lifecycle_event_type", type_="check")
        batch.create_check_constraint(
            "ck_job_lifecycle_event_type",
            "event_type IN ('first_seen', 'changed', 'missing', 'recovered', "
            "'closed', 'reopened')",
        )
    with op.batch_alter_table("job_versions") as batch:
        batch.drop_column("fact_contract_version")

    _create_analysis_views()
