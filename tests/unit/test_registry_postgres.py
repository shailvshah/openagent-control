from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.db.tables import AgentRow, Base, OperatorActionRow
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.domain.errors import AgentNotFoundError
from openagent_control.domain.models import AgentPatch, AgentStatus, RegisteredAgent, RiskTier


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


def _new_agent(spiffe_id: str = "spiffe://corp.net/ns/finance/agent/new-bot") -> RegisteredAgent:
    return RegisteredAgent(
        spiffe_id=spiffe_id,
        display_name="New Bot",
        purpose="demo",
        owner="alice@corp.net",
        risk_tier=RiskTier.LOW,
        granted_tools=["read_query"],
    )


@pytest.mark.asyncio
async def test_create_persists_agent_and_audit_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)
    agent = _new_agent()

    created = await registry.create(agent, operator_subject="alice@corp.net")

    assert created == agent
    looked_up = await registry.lookup(agent.spiffe_id)
    assert looked_up is not None
    assert looked_up.spiffe_id == agent.spiffe_id
    assert looked_up.owner == agent.owner
    assert looked_up.granted_tools == agent.granted_tools

    async with session_factory() as session:
        audit_rows = (await session.execute(select(OperatorActionRow))).scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "agent.create"
    assert audit_rows[0].operator_subject == "alice@corp.net"
    assert audit_rows[0].target_spiffe_id == agent.spiffe_id


@pytest.mark.asyncio
async def test_list_agents_filters_by_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)
    active = _new_agent("spiffe://corp.net/ns/finance/agent/active-bot")
    await registry.create(active, operator_subject="alice@corp.net")
    await registry.create(
        _new_agent("spiffe://corp.net/ns/finance/agent/suspended-bot"),
        operator_subject="alice@corp.net",
    )
    await registry.set_status(
        "spiffe://corp.net/ns/finance/agent/suspended-bot",
        AgentStatus.SUSPENDED,
        operator_subject="alice@corp.net",
    )

    all_agents = await registry.list_agents()
    active_only = await registry.list_agents(status=AgentStatus.ACTIVE)

    assert {a.spiffe_id for a in all_agents} == {
        "spiffe://corp.net/ns/finance/agent/active-bot",
        "spiffe://corp.net/ns/finance/agent/suspended-bot",
    }
    assert [a.spiffe_id for a in active_only] == ["spiffe://corp.net/ns/finance/agent/active-bot"]


@pytest.mark.asyncio
async def test_update_patches_only_provided_fields_and_audits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)
    agent = _new_agent()
    await registry.create(agent, operator_subject="alice@corp.net")

    updated = await registry.update(
        agent.spiffe_id,
        AgentPatch(
            owner="bob@corp.net",
            risk_tier=RiskTier.HIGH,
            purpose="updated purpose",
            granted_tools=["read_query", "write_query"],
        ),
        operator_subject="bob@corp.net",
    )

    assert updated.owner == "bob@corp.net"
    assert updated.risk_tier is RiskTier.HIGH
    assert updated.purpose == "updated purpose"
    assert updated.granted_tools == ["read_query", "write_query"]
    assert updated.display_name == agent.display_name  # untouched field preserved
    assert updated.updated_at > agent.updated_at

    async with session_factory() as session:
        audit_rows = (await session.execute(select(OperatorActionRow))).scalars().all()
    update_rows = [row for row in audit_rows if row.action == "agent.update"]
    assert len(update_rows) == 1
    assert update_rows[0].operator_subject == "bob@corp.net"


@pytest.mark.asyncio
async def test_update_unknown_agent_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)

    with pytest.raises(AgentNotFoundError):
        await registry.update(
            "spiffe://corp.net/ns/x/agent/ghost", AgentPatch(owner="x"), operator_subject="alice"
        )


@pytest.mark.asyncio
async def test_set_status_updates_status_changed_at_and_audits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)
    agent = _new_agent()
    await registry.create(agent, operator_subject="alice@corp.net")

    suspended = await registry.set_status(
        agent.spiffe_id, AgentStatus.SUSPENDED, operator_subject="alice@corp.net"
    )

    assert suspended.status is AgentStatus.SUSPENDED
    assert suspended.status_changed_at is not None

    async with session_factory() as session:
        audit_rows = (await session.execute(select(OperatorActionRow))).scalars().all()
    status_rows = [row for row in audit_rows if row.action == "agent.set_status"]
    assert status_rows[0].detail == {"from": "active", "to": "suspended"}


@pytest.mark.asyncio
async def test_set_status_unknown_agent_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = PostgresAgentRegistry(session_factory)

    with pytest.raises(AgentNotFoundError):
        await registry.set_status(
            "spiffe://corp.net/ns/x/agent/ghost", AgentStatus.SUSPENDED, operator_subject="alice"
        )
