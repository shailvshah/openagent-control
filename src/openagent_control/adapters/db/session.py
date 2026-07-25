"""Async SQLAlchemy engine/session factory shared by the Postgres adapters."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openagent_control.adapters.db.tables import SCHEMA


def make_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    if database_url.startswith("sqlite"):
        # SQLite has no schema concept equivalent to Postgres's CREATE SCHEMA;
        # map the `oac` schema declared on Base.metadata down to "no schema" so
        # the same ORM models work against an in-memory SQLite test database.
        engine = engine.execution_options(schema_translate_map={SCHEMA: None})
    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
