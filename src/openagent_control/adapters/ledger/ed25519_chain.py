"""Ed25519 hash-chained ledger adapter. See docs/adr/0003-ed25519-hash-chained-audit-ledger.md.

Known v1 limitations (tracked in the ADR, not solved here):
- the signing key is generated in-process and lost on restart;
- chain state (`_previous_hash`) lives in a single process's memory, so this does
  not scale to multiple replicas without a shared, race-safe store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openagent_control.domain.models import (
    AgentIdentity,
    Decision,
    ExecutionReceipt,
    PolicyDecision,
    ToolCallRequest,
)

_GENESIS_HASH = "0" * 64


def _canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, default=str).encode("utf-8")


class Ed25519ChainLedger:
    def __init__(self, private_key: Ed25519PrivateKey | None = None) -> None:
        self._private_key = private_key or Ed25519PrivateKey.generate()
        self._previous_hash = _GENESIS_HASH
        self._lock = asyncio.Lock()

    async def record(
        self, agent: AgentIdentity, request: ToolCallRequest, decision: PolicyDecision
    ) -> ExecutionReceipt:
        payload_hash = hashlib.sha256(_canonical_json(request.model_dump(mode="json"))).hexdigest()

        async with self._lock:
            receipt = ExecutionReceipt(
                sequence_id=str(uuid.uuid4()),
                spiffe_id=agent.spiffe_id,
                decision=Decision(decision.decision),
                reason=decision.reason,
                payload_hash=payload_hash,
                previous_hash=self._previous_hash,
            )
            unsigned_bytes = _canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
            signature = self._private_key.sign(unsigned_bytes)
            receipt.signature = signature.hex()

            self._previous_hash = hashlib.sha256(unsigned_bytes + signature).hexdigest()

        return receipt
