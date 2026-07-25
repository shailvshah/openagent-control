"""make_engine's schema-translation branch selection (ADR-0009).

Engine construction is lazy (no connection attempt), so this is safe to test
against a postgresql+asyncpg URL without a running Postgres server.
"""

from __future__ import annotations

from openagent_control.adapters.db.session import make_engine
from openagent_control.adapters.db.tables import SCHEMA


def test_sqlite_url_gets_schema_translate_map() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")

    assert engine.get_execution_options().get("schema_translate_map") == {SCHEMA: None}


def test_postgres_url_keeps_real_schema() -> None:
    engine = make_engine("postgresql+asyncpg://user:pass@localhost/db")

    assert "schema_translate_map" not in engine.get_execution_options()
