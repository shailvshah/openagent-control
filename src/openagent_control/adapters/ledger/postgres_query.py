"""Read-only access to the Postgres-backed audit ledger, for the control
plane. See docs/adr/0014.

Deliberately a separate class from PostgresLedger (adapters/ledger/postgres.py),
which is the only thing that can write oac.execution_receipts. This class
takes a bare Ed25519PublicKey, never a Signer — there is no code path here
capable of producing a valid signature, by construction, even under full
compromise of the control-plane process.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.tables import ReceiptRow
from openagent_control.adapters.ledger.signing import GENESIS_HASH, canonical_json
from openagent_control.domain.models import ChainVerificationResult, Decision, ExecutionReceipt


def _to_domain(row: ReceiptRow) -> ExecutionReceipt:
    # SQLite (used in tests, see adapters/db/session.py) returns a naive
    # datetime on round-trip even for a DateTime(timezone=True) column;
    # Postgres's timestamptz does not. A naive vs. aware datetime serializes
    # differently under model_dump(mode="json") ("+00:00" suffix or not),
    # which would make verify_chain's recomputed signature never match a
    # genuine one purely from this backend quirk — normalize to UTC-aware.
    timestamp = row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=UTC)
    return ExecutionReceipt(
        sequence_id=row.sequence_id,
        timestamp=timestamp,
        spiffe_id=row.spiffe_id,
        decision=Decision(row.decision),
        reason=row.reason,
        payload_hash=row.payload_hash,
        previous_hash=row.previous_hash,
        signature=row.signature,
        enforced=row.enforced,
    )


class PostgresReceiptQuery:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], public_key: Ed25519PublicKey
    ) -> None:
        self._session_factory = session_factory
        self._public_key = public_key

    async def search(
        self,
        *,
        spiffe_id: str | None = None,
        decision: Decision | None = None,
        enforced: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionReceipt]:
        stmt = select(ReceiptRow).order_by(ReceiptRow.id.desc()).limit(limit).offset(offset)
        if spiffe_id is not None:
            stmt = stmt.where(ReceiptRow.spiffe_id == spiffe_id)
        if decision is not None:
            stmt = stmt.where(ReceiptRow.decision == decision.value)
        if enforced is not None:
            stmt = stmt.where(ReceiptRow.enforced == enforced)
        if since is not None:
            stmt = stmt.where(ReceiptRow.timestamp >= since)
        if until is not None:
            stmt = stmt.where(ReceiptRow.timestamp <= until)

        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def get(self, sequence_id: str) -> ExecutionReceipt | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ReceiptRow).where(ReceiptRow.sequence_id == sequence_id)
                )
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def verify_chain(self) -> ChainVerificationResult:
        """Walks every receipt in insertion order, recomputing each hash link
        and signature. O(n) over the whole table — never call from a hot path."""
        async with self._session_factory() as session:
            rows = (
                (await session.execute(select(ReceiptRow).order_by(ReceiptRow.id))).scalars().all()
            )

        expected_previous_hash = GENESIS_HASH
        checked = 0
        for row in rows:
            receipt = _to_domain(row)
            if receipt.previous_hash != expected_previous_hash:
                return ChainVerificationResult(
                    valid=False,
                    receipts_checked=checked,
                    first_broken_sequence_id=receipt.sequence_id,
                )

            unsigned_bytes = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
            signature = bytes.fromhex(receipt.signature or "")
            try:
                self._public_key.verify(signature, unsigned_bytes)
            except InvalidSignature:
                return ChainVerificationResult(
                    valid=False,
                    receipts_checked=checked,
                    first_broken_sequence_id=receipt.sequence_id,
                )

            expected_previous_hash = hashlib.sha256(unsigned_bytes + signature).hexdigest()
            checked += 1

        return ChainVerificationResult(valid=True, receipts_checked=checked)
