"""Shared receipt-signing logic used by every ledger adapter.

Extracted so the in-memory (Ed25519ChainLedger) and Postgres (PostgresLedger)
adapters sign identically instead of duplicating canonical-JSON code — see
ADR-0009.

`Signer` is the seam ADR-0013 plugs a KMS-backed signer into: `ReceiptSigner`
holds the private key in-process (the v1 default, and still the right choice
for local dev — see ADR-0005's stance on dev stubs), while
`adapters/ledger/vault_signer.py` never lets the key material leave Vault at
all. Both satisfy this Protocol structurally, so `Ed25519ChainLedger` and
`PostgresLedger` don't know or care which one they were given.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS_HASH = "0" * 64


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, default=str).encode("utf-8")


@runtime_checkable
class Signer(Protocol):
    """Signs receipt bytes and exposes the corresponding public key.

    Deliberately narrower than "holds a private key": a KMS-backed
    implementation must be able to satisfy this without ever exposing key
    material to the process — see ADR-0013.
    """

    def sign(self, unsigned_bytes: bytes) -> bytes: ...
    def public_key(self) -> Ed25519PublicKey: ...


class ReceiptSigner:
    """The v1 default: an Ed25519 key held in this process's memory.

    Regenerated on restart unless a key is explicitly injected — see
    ADR-0003's stated limitation and ADR-0013 for the KMS-backed alternative.
    """

    def __init__(self, private_key: Ed25519PrivateKey | None = None) -> None:
        self._private_key = private_key or Ed25519PrivateKey.generate()

    def sign(self, unsigned_bytes: bytes) -> bytes:
        return self._private_key.sign(unsigned_bytes)

    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()
