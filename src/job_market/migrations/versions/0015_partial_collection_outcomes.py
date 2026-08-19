"""Persist partial collection outcomes and structured issue details.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch:
        batch.add_column(
            sa.Column(
                "issues",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.create_check_constraint(
            "ck_crawl_runs_status",
            "status IN ('running', 'success', 'partial', 'failed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch:
        batch.drop_constraint("ck_crawl_runs_status", type_="check")
        batch.drop_column("issues")
