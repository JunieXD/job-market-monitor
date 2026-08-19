"""Record whether a crawl can authoritatively infer absent jobs.

Revision ID: 0013
Revises: 0012

A live list can change during pagination. Those rows are useful observations,
but a nearly complete walk must not close jobs that happened to be omitted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch:
        batch.add_column(
            sa.Column(
                "absence_authoritative",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # Before this distinction existed, every persisted successful complete
    # run came from a connector that claimed full source-channel coverage.
    op.execute(
        sa.text(
            "UPDATE crawl_runs SET absence_authoritative = true "
            "WHERE status = 'success' AND complete = true"
        )
    )

    with op.batch_alter_table("crawl_runs") as batch:
        batch.alter_column(
            "absence_authoritative",
            existing_type=sa.Boolean(),
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch:
        batch.drop_column("absence_authoritative")
