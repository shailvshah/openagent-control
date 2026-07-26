"""PostgresReceiptQuery against an in-memory SQLite database — same real
SQLAlchemy code path as test_ledger_postgres.py, without a running Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.db.tables import Base, ChainStateRow
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.ledger.postgres_query import PostgresReceiptQuery
from openagent_control.adapters.ledger.signing import GENESIS_HASH, ReceiptSigner
from openagent_control.domain.models import AgentIdentity, Decision, PolicyDecision, ToolCallRequest


def _call() -> ToolCallRequest:
    return ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={"table": "invoices"},
        agent=AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot"),
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        session.add(ChainStateRow(id=1, previous_hash=GENESIS_HASH))
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_returns_receipts_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")
    first = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
    second = await ledger.record(
        agent, _call(), PolicyDecision(decision=Decision.DENY, reason="velocity_limit")
    )

    query = PostgresReceiptQuery(session_factory, signer.public_key())
    results = await query.search()

    assert [r.sequence_id for r in results] == [second.sequence_id, first.sequence_id]


@pytest.mark.asyncio
async def test_search_filters_by_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")
    await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
    denied = await ledger.record(
        agent, _call(), PolicyDecision(decision=Decision.DENY, reason="nope")
    )

    query = PostgresReceiptQuery(session_factory, signer.public_key())
    results = await query.search(decision=Decision.DENY)

    assert [r.sequence_id for r in results] == [denied.sequence_id]


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_sequence_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = ReceiptSigner()
    query = PostgresReceiptQuery(session_factory, signer.public_key())

    assert await query.get("no-such-sequence-id") is None


@pytest.mark.asyncio
async def test_verify_chain_is_valid_for_an_intact_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")
    await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
    await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))

    query = PostgresReceiptQuery(session_factory, signer.public_key())
    result = await query.verify_chain()

    assert result.valid is True
    assert result.receipts_checked == 2
    assert result.first_broken_sequence_id is None


@pytest.mark.asyncio
async def test_verify_chain_detects_a_tampered_receipt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select

    from openagent_control.adapters.db.tables import ReceiptRow

    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")
    receipt = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))

    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                select(ReceiptRow).where(ReceiptRow.sequence_id == receipt.sequence_id)
            )
        ).scalar_one()
        row.reason = "tampered"

    query = PostgresReceiptQuery(session_factory, signer.public_key())
    result = await query.verify_chain()

    assert result.valid is False
    assert result.first_broken_sequence_id == receipt.sequence_id
