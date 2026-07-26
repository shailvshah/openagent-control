"""initial schema: oac.agents, oac.execution_receipts, oac.chain_state

Revision ID: 0001
Revises:
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from openagent_control.adapters.db.tables import SCHEMA

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "agents",
        sa.Column("spiffe_id", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("risk_tier", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("granted_tools", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.create_table(
        "execution_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # unique=True already gives sequence_id a unique index in Postgres; a
        # separate op.create_index on the same column would be a second,
        # redundant index paying write amplification on every receipt insert.
        sa.Column("sequence_id", sa.String(), nullable=False, unique=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spiffe_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("previous_hash", sa.String(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_execution_receipts_spiffe_id", "execution_receipts", ["spiffe_id"], schema=SCHEMA
    )

    op.create_table(
        "chain_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("previous_hash", sa.String(), nullable=False),
        schema=SCHEMA,
    )
    op.bulk_insert(
        sa.table(
            "chain_state",
            sa.column("id", sa.Integer),
            sa.column("previous_hash", sa.String),
            schema=SCHEMA,
        ),
        [{"id": 1, "previous_hash": "0" * 64}],
    )


def downgrade() -> None:
    op.drop_table("chain_state", schema=SCHEMA)
    op.drop_index("ix_execution_receipts_spiffe_id", table_name="execution_receipts", schema=SCHEMA)
    op.drop_table("execution_receipts", schema=SCHEMA)
    op.drop_table("agents", schema=SCHEMA)
