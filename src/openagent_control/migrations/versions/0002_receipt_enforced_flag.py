"""add execution_receipts.enforced

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from openagent_control.adapters.db.tables import SCHEMA

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # server_default so every existing row backfills to True (enforced) — a
    # receipt written before shadow mode existed was, by definition, enforced.
    op.add_column(
        "execution_receipts",
        sa.Column("enforced", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("execution_receipts", "enforced", schema=SCHEMA)
