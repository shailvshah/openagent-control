"""SQLAlchemy table definitions backing the Postgres registry and ledger (ADR-0009).

Plain `JSON` (not Postgres `JSONB`) so the same schema runs against SQLite in
tests without a real Postgres server. `Alembic` migrations in migrations/ create
these tables against whatever database the operator points `database_url` at.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "oac"


class Base(DeclarativeBase):
    """All tables live in the `oac` Postgres schema, not `public` — keeps the
    control plane's tables isolated from whatever else lives in the operator's
    database. The Alembic migration creates the schema (`CREATE SCHEMA IF NOT
    EXISTS oac`) before creating tables in it. Tests run against SQLite via a
    schema_translate_map (see adapters/db/session.py) since SQLite has no
    equivalent concept."""

    metadata = MetaData(schema=SCHEMA)


class AgentRow(Base):
    __tablename__ = "agents"

    spiffe_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    risk_tier: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    granted_tools: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    """Each element is a `ToolGrant` dict (`{"name": ..., ...}`, ADR-0021) once
    written by this codebase; a legacy row may still hold a plain string until
    next updated — `RegisteredAgent`'s validator normalizes either shape."""
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_changed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReceiptRow(Base):
    __tablename__ = "execution_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sequence_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spiffe_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String, nullable=False)
    signature: Mapped[str] = mapped_column(String, nullable=False)
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """False for a shadow-mode (decision_mode="observe") DENY that was recorded
    but not actually blocked. See ADR-0012."""


class ChainStateRow(Base):
    """Singleton row (id=1) holding the current chain head. Locked with
    SELECT ... FOR UPDATE inside the same transaction as a receipt insert so
    concurrent writers across replicas serialize instead of racing."""

    __tablename__ = "chain_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    previous_hash: Mapped[str] = mapped_column(String, nullable=False)


class OperatorActionRow(Base):
    """Audit trail of the control plane's own mutating actions (ADR-0014):
    every agent create/update/status-change writes one row here in the same
    transaction as its `agents` write. An admin surface with no record of its
    own actions would be a real gap in a project whose whole pitch is
    auditability."""

    __tablename__ = "operator_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_subject: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_spiffe_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
