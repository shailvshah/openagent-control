"""PostgresLedger against an in-memory SQLite database (schema-translated, see
adapters/db/session.py) — exercises the real SQLAlchemy code path without
requiring a running Postgres server for the test suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.db.tables import Base, ChainStateRow
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.ledger.signing import GENESIS_HASH, ReceiptSigner, canonical_json
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
async def test_first_receipt_chains_from_genesis(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ledger = PostgresLedger(session_factory, ReceiptSigner())
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

    receipt = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))

    assert receipt.previous_hash == GENESIS_HASH
    assert receipt.decision is Decision.ALLOW

    public_key = ledger.public_key()
    unsigned = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
    public_key.verify(bytes.fromhex(receipt.signature or ""), unsigned)


@pytest.mark.asyncio
async def test_second_receipt_chains_from_first_and_is_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select

    from openagent_control.adapters.db.tables import ReceiptRow

    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

    first = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
    second = await ledger.record(
        agent, _call(), PolicyDecision(decision=Decision.DENY, reason="velocity_limit")
    )

    assert second.previous_hash != first.previous_hash
    assert second.previous_hash != GENESIS_HASH

    async with session_factory() as session:
        rows = (await session.execute(select(ReceiptRow))).scalars().all()

    assert len(rows) == 2
    assert {row.sequence_id for row in rows} == {first.sequence_id, second.sequence_id}
    assert next(r for r in rows if r.sequence_id == second.sequence_id).reason == "velocity_limit"


@pytest.mark.asyncio
async def test_public_key_matches_signer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    signer = ReceiptSigner()
    ledger = PostgresLedger(session_factory, signer)

    assert ledger.public_key().public_bytes_raw() == signer.public_key().public_bytes_raw()
