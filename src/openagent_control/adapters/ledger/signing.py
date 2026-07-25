"""Shared Ed25519 receipt-signing logic used by every ledger adapter.

Extracted so the in-memory (Ed25519ChainLedger) and Postgres (PostgresLedger)
adapters sign identically instead of duplicating canonical-JSON/crypto code —
see ADR-0009.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS_HASH = "0" * 64


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, default=str).encode("utf-8")


class ReceiptSigner:
    def __init__(self, private_key: Ed25519PrivateKey | None = None) -> None:
        self._private_key = private_key or Ed25519PrivateKey.generate()

    def sign(self, unsigned_bytes: bytes) -> bytes:
        return self._private_key.sign(unsigned_bytes)

    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()
