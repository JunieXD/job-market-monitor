"""Disable the provisional keyword-topic experiment.

Revision ID: 0005
Revises: 0004

The experiment is retained in the normalized tables for audit, but no run is
published and no topic view is exposed until a taxonomy and evaluation policy
have been approved.
"""

from collections.abc import Sequence
from importlib import import_module

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS daily_market_topic_stats")
    op.execute("DROP VIEW IF EXISTS daily_topic_stats")
    op.execute(
        "UPDATE derivation_runs SET is_current = false "
        "WHERE extractor_name = 'topic-keywords' AND is_current"
    )
    op.execute(
        "UPDATE topics SET active = false "
        "WHERE taxonomy_version = 'v1' AND key = 'agent'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE topics SET active = true "
        "WHERE taxonomy_version = 'v1' AND key = 'agent'"
    )
    op.execute(
        "UPDATE derivation_runs SET is_current = true "
        "WHERE id = ("
        "SELECT id FROM derivation_runs "
        "WHERE extractor_name = 'topic-keywords' AND status = 'success' "
        "ORDER BY finished_at DESC, id DESC LIMIT 1"
        ")"
    )
    previous = import_module(
        "job_market.migrations.versions.0004_topic_relevance"
    )
    op.execute(previous.TOPIC_VIEW)
    op.execute(previous.MARKET_TOPIC_VIEW)
