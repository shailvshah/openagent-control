"""Postgres-backed hash-chained ledger. See docs/adr/0009.

Closes both gaps ADR-0003 flagged: the signing key is provided by the caller
(sourced from wherever the operator's KMS/secret store puts it — this adapter
does not generate or persist it), and chain state is a database row, correct
across multiple gateway replicas because `record()` takes a row lock on the
chain head inside the same transaction as the receipt insert.
"""

from __future__ import annotations

import hashlib
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.tables import ChainStateRow, ReceiptRow
from openagent_control.adapters.ledger.signing import ReceiptSigner, canonical_json
from openagent_control.domain.models import (
    AgentIdentity,
    ExecutionReceipt,
    PolicyDecision,
    ToolCallRequest,
)


class PostgresLedger:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], signer: ReceiptSigner
    ) -> None:
        self._session_factory = session_factory
        self._signer = signer

    def public_key(self) -> Ed25519PublicKey:
        return self._signer.public_key()

    async def record(
        self, agent: AgentIdentity, request: ToolCallRequest, decision: PolicyDecision
    ) -> ExecutionReceipt:
        payload_hash = hashlib.sha256(canonical_json(request.model_dump(mode="json"))).hexdigest()

        async with self._session_factory() as session, session.begin():
            chain_state = (
                await session.execute(
                    select(ChainStateRow).where(ChainStateRow.id == 1).with_for_update()
                )
            ).scalar_one()

            receipt = ExecutionReceipt(
                sequence_id=str(uuid.uuid4()),
                spiffe_id=agent.spiffe_id,
                decision=decision.decision,
                reason=decision.reason,
                payload_hash=payload_hash,
                previous_hash=chain_state.previous_hash,
            )
            unsigned_bytes = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
            signature = self._signer.sign(unsigned_bytes)
            receipt.signature = signature.hex()

            session.add(
                ReceiptRow(
                    sequence_id=receipt.sequence_id,
                    timestamp=receipt.timestamp,
                    spiffe_id=receipt.spiffe_id,
                    decision=receipt.decision.value,
                    reason=receipt.reason,
                    payload_hash=receipt.payload_hash,
                    previous_hash=receipt.previous_hash,
                    signature=receipt.signature,
                )
            )
            chain_state.previous_hash = hashlib.sha256(unsigned_bytes + signature).hexdigest()

        return receipt
