"""Ed25519 receipt signing via HashiCorp Vault's Transit secrets engine.

See ADR-0013 for why Vault specifically: AWS KMS and Azure Key Vault's
asymmetric-signing APIs support RSA and ECDSA, not Ed25519 — using either
would mean breaking ADR-0003's algorithm choice. Vault's Transit engine signs
Ed25519 natively, and the private key material never leaves Vault: `sign()`
posts the payload and gets a signature back over HTTPS, and this process never
holds, generates, or has the ability to export the key.

Uses Vault's plain HTTP API directly (`httpx`, already a core dependency)
rather than the `hvac` SDK — the surface needed here is two endpoints, and
adding an SDK dependency for that trades very little complexity for a new
transitive dependency tree.
"""

from __future__ import annotations

import base64

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class VaultSignerError(Exception):
    """Raised when Vault rejects a request or returns something unexpected."""


class VaultTransitSigner:
    def __init__(
        self,
        vault_url: str,
        token: str,
        key_name: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = vault_url.rstrip("/")
        self._key_name = key_name
        owns_client = client is None
        self._client = client or httpx.Client(headers={"X-Vault-Token": token}, timeout=10.0)
        try:
            # Fetched once at construction (startup), matching
            # OidcJwksIdentityProvider's posture (ADR-0010): an unreachable
            # Vault or a missing transit key becomes a startup failure, not a
            # per-request one.
            self._public_key = self._fetch_public_key()
        except Exception:
            if owns_client:
                self._client.close()
            raise

    def _fetch_public_key(self) -> Ed25519PublicKey:
        response = self._client.get(f"{self._base}/v1/transit/keys/{self._key_name}")
        if response.status_code == 404:
            raise VaultSignerError(
                f"transit key '{self._key_name}' does not exist — create it with: "
                f"vault write -f transit/keys/{self._key_name} type=ed25519"
            )
        response.raise_for_status()
        keys = response.json()["data"]["keys"]
        latest = keys[str(max(int(v) for v in keys))]
        raw = base64.b64decode(latest["public_key"])
        return Ed25519PublicKey.from_public_bytes(raw)

    def sign(self, unsigned_bytes: bytes) -> bytes:
        payload = base64.b64encode(unsigned_bytes).decode()
        response = self._client.post(
            f"{self._base}/v1/transit/sign/{self._key_name}", json={"input": payload}
        )
        response.raise_for_status()
        # Vault's signature field is "vault:v<key-version>:<base64 signature>".
        signature_field = response.json()["data"]["signature"]
        signature_b64 = signature_field.rsplit(":", 1)[-1]
        return base64.b64decode(signature_b64)

    def public_key(self) -> Ed25519PublicKey:
        return self._public_key
