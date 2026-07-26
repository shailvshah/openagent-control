"""Diagnostics against real Postgres and Redis.

The schema check is the one that matters: an un-migrated database *connects
perfectly* and then fails every query. Asserting that against a mock would only
prove the mock returns what we told it to, so these run against real servers.

Opt-in:

    OAC_TEST_DATABASE_URL=postgresql+asyncpg://oac@127.0.0.1:5433/oac \\
    OAC_TEST_REDIS_URL=redis://127.0.0.1:6380/0 \\
    poetry run pytest tests/integration/test_diagnostics_backends.py
"""

from __future__ import annotations

import os

import pytest

from openagent_control import diagnostics
from openagent_control.config import Settings

DATABASE_URL = os.environ.get("OAC_TEST_DATABASE_URL", "")
REDIS_URL = os.environ.get("OAC_TEST_REDIS_URL", "")


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="set OAC_TEST_DATABASE_URL")
async def test_database_check_reports_schema_at_head() -> None:
    """Requires `openagent-control migrate` to have been run against this DB."""
    check = await diagnostics.check_database(Settings(database_url=DATABASE_URL))

    assert check.ok, check.detail
    assert "schema at head" in check.detail


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="set OAC_TEST_DATABASE_URL")
async def test_database_check_fails_on_an_unmigrated_database() -> None:
    """A database with no oac schema connects fine — that is the trap."""
    unmigrated = DATABASE_URL.rsplit("/", 1)[0] + "/postgres"

    check = await diagnostics.check_database(Settings(database_url=unmigrated))

    assert not check.ok
    assert "openagent-control migrate" in check.detail


@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="set OAC_TEST_DATABASE_URL")
async def test_run_all_reports_an_unreachable_database_without_raising() -> None:
    settings = Settings(database_url="postgresql+asyncpg://nobody@127.0.0.1:1/nope")

    checks = await diagnostics.run_all(settings)

    database = next(c for c in checks if c.name == "database")
    assert not database.ok


@pytest.mark.asyncio
@pytest.mark.skipif(not REDIS_URL, reason="set OAC_TEST_REDIS_URL")
async def test_redis_check_pings_a_real_server() -> None:
    check = await diagnostics.check_redis(Settings(redis_url=REDIS_URL))

    assert check.ok and check.detail == "ping ok"


@pytest.mark.asyncio
@pytest.mark.skipif(not REDIS_URL, reason="set OAC_TEST_REDIS_URL")
async def test_redis_check_fails_when_unreachable() -> None:
    checks = await diagnostics.run_all(Settings(redis_url="redis://127.0.0.1:1/0"))

    redis = next(c for c in checks if c.name == "redis")
    assert not redis.ok
