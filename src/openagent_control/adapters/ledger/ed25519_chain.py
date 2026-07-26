"""In-memory Ed25519 hash-chained ledger adapter. See docs/adr/0003.

Known v1 limitations (tracked in the ADR, not solved here — PostgresLedger in
adapters/ledger/postgres.py addresses the second; ADR-0013 addresses the first
via a Vault-backed Signer):
- the default signing key is generated in-process and lost on restart;
- chain state (`_previous_hash`) lives in a single process's memory, so this does
  not scale to multiple replicas without a shared, race-safe store.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openagent_control.adapters.ledger.signing import (
    GENESIS_HASH,
    ReceiptSigner,
    Signer,
    canonical_json,
)
from openagent_control.domain.models import (
    AgentIdentity,
    ExecutionReceipt,
    PolicyDecision,
    ToolCallRequest,
)


class Ed25519ChainLedger:
    def __init__(self, signer: Signer | None = None) -> None:
        self._signer = signer or ReceiptSigner()
        self._previous_hash = GENESIS_HASH
        self._lock = asyncio.Lock()

    def public_key(self) -> Ed25519PublicKey:
        """The verification key for receipts signed by this ledger instance."""
        return self._signer.public_key()

    async def record(
        self,
        agent: AgentIdentity,
        request: ToolCallRequest,
        decision: PolicyDecision,
        *,
        enforced: bool = True,
    ) -> ExecutionReceipt:
        payload_hash = hashlib.sha256(canonical_json(request.model_dump(mode="json"))).hexdigest()

        async with self._lock:
            receipt = ExecutionReceipt(
                sequence_id=str(uuid.uuid4()),
                spiffe_id=agent.spiffe_id,
                decision=decision.decision,
                reason=decision.reason,
                payload_hash=payload_hash,
                previous_hash=self._previous_hash,
                enforced=enforced,
            )
            unsigned_bytes = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
            signature = self._signer.sign(unsigned_bytes)
            receipt.signature = signature.hex()

            self._previous_hash = hashlib.sha256(unsigned_bytes + signature).hexdigest()

        return receipt
