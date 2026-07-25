from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.db.tables import AgentRow, Base
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.domain.models import AgentStatus, RiskTier


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_lookup_returns_registered_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.datetime.now(datetime.UTC)
    async with session_factory() as session, session.begin():
        session.add(
            AgentRow(
                spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot",
                display_name="Invoice Bot",
                purpose="demo",
                owner="alice@corp.net",
                risk_tier="medium",
                status="active",
                granted_tools=["read_query"],
                created_at=now,
                updated_at=now,
            )
        )

    registry = PostgresAgentRegistry(session_factory)
    agent = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")

    assert agent is not None
    assert agent.owner == "alice@corp.net"
    assert agent.risk_tier is RiskTier.MEDIUM
    assert agent.status is AgentStatus.ACTIVE
    assert agent.granted_tools == ["read_query"]


@pytest.mark.asyncio
async def test_lookup_unknown_agent_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)

    assert await registry.lookup("spiffe://corp.net/ns/x/agent/ghost") is None


@pytest.mark.asyncio
async def test_lookup_reflects_suspended_status_and_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.datetime.now(datetime.UTC)
    async with session_factory() as session, session.begin():
        session.add(
            AgentRow(
                spiffe_id="spiffe://corp.net/ns/x/agent/retired-bot",
                display_name="Retired Bot",
                purpose="demo",
                owner="bob@corp.net",
                risk_tier="high",
                status="suspended",
                granted_tools=[],
                created_at=now,
                updated_at=now,
                status_changed_at=now,
            )
        )

    registry = PostgresAgentRegistry(session_factory)
    agent = await registry.lookup("spiffe://corp.net/ns/x/agent/retired-bot")

    assert agent is not None
    assert agent.status is AgentStatus.SUSPENDED
    assert agent.status_changed_at is not None
