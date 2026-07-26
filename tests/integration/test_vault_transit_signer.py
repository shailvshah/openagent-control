"""VaultTransitSigner against a real HashiCorp Vault dev server.

Same standard the rest of this repo holds integration work to: verify against
the real thing, not a hand-written stand-in for it. A fake HTTP server built
by whoever wrote the adapter tends to share the adapter's own assumptions
about Vault's response shapes — exactly the class of bug the Keycloak and
GitHub MCP conformance suites caught elsewhere in this project.

Skips if the `vault` binary is unavailable, the same posture the OPA-backed
integration tests take: right locally, and CI installs the real binary rather
than faking the dependency away.
"""

from __future__ import annotations

import base64
import contextlib
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.signing import canonical_json
from openagent_control.adapters.ledger.vault_signer import VaultSignerError, VaultTransitSigner
from openagent_control.config import Settings
from openagent_control.diagnostics import check_signing_key, run_all
from openagent_control.domain.models import AgentIdentity, Decision, PolicyDecision, ToolCallRequest
from openagent_control.gateway.dependencies import build_container

_ROOT_TOKEN = "test-root-token"
_KEY_NAME = "oac-test-signer"

pytestmark = pytest.mark.skipif(
    shutil.which("vault") is None,
    reason="requires the real `vault` binary: brew install hashicorp/tap/vault",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.contextmanager
def run_vault() -> Iterator[str]:
    """Starts a real `vault server -dev`, enables Transit, and creates an
    Ed25519 key. Yields the base URL."""
    port = _free_port()
    process = subprocess.Popen(
        [
            "vault",
            "server",
            "-dev",
            f"-dev-root-token-id={_ROOT_TOKEN}",
            f"-dev-listen-address=127.0.0.1:{port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Vault-Token": _ROOT_TOKEN}
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{base}/v1/sys/health", timeout=1.0).status_code < 500:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("vault dev server did not become healthy")

        httpx.post(
            f"{base}/v1/sys/mounts/transit", headers=headers, json={"type": "transit"}, timeout=10.0
        ).raise_for_status()
        httpx.post(
            f"{base}/v1/transit/keys/{_KEY_NAME}",
            headers=headers,
            json={"type": "ed25519"},
            timeout=10.0,
        ).raise_for_status()

        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def _call() -> ToolCallRequest:
    return ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={"table": "invoices"},
        agent=AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot"),
    )


def test_public_key_matches_a_real_transit_key() -> None:
    with run_vault() as vault_url:
        signer = VaultTransitSigner(vault_url, _ROOT_TOKEN, _KEY_NAME)

        raw = signer.public_key().public_bytes_raw()
        assert len(raw) == 32  # a real Ed25519 public key, not a placeholder


def test_signature_verifies_independently_against_vaults_own_public_key() -> None:
    """The whole point: a signature this adapter produces must be checkable
    by ordinary Ed25519 verification, with no Vault involvement at all."""
    with run_vault() as vault_url:
        signer = VaultTransitSigner(vault_url, _ROOT_TOKEN, _KEY_NAME)

        message = b"receipt bytes to be signed"
        signature = signer.sign(message)

        assert len(signature) == 64  # a real Ed25519 signature, not base64 text
        signer.public_key().verify(signature, message)  # raises on failure


def test_tampered_message_fails_verification() -> None:
    with run_vault() as vault_url:
        signer = VaultTransitSigner(vault_url, _ROOT_TOKEN, _KEY_NAME)
        signature = signer.sign(b"original")

        with pytest.raises(InvalidSignature):
            signer.public_key().verify(signature, b"tampered")


def test_missing_key_raises_an_actionable_error() -> None:
    with run_vault() as vault_url, pytest.raises(VaultSignerError, match="does not exist"):
        VaultTransitSigner(vault_url, _ROOT_TOKEN, "no-such-key")


def test_wrong_token_is_refused() -> None:
    with run_vault() as vault_url, pytest.raises(httpx.HTTPStatusError):
        VaultTransitSigner(vault_url, "not-the-real-token", _KEY_NAME)


@pytest.mark.asyncio
async def test_ed25519_chain_ledger_signs_and_chains_via_vault() -> None:
    """The full point of ADR-0013's Signer Protocol: a Vault-backed signer
    plugs into the same ledger adapter with zero special-casing."""
    with run_vault() as vault_url:
        signer = VaultTransitSigner(vault_url, _ROOT_TOKEN, _KEY_NAME)
        ledger = Ed25519ChainLedger(signer)
        agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

        first = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
        second = await ledger.record(
            agent, _call(), PolicyDecision(decision=Decision.DENY, reason="not granted")
        )

        assert second.previous_hash != first.previous_hash

        public_key: Ed25519PublicKey = ledger.public_key()
        for receipt in (first, second):
            unsigned = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
            public_key.verify(bytes.fromhex(receipt.signature or ""), unsigned)


def test_build_container_wires_a_working_vault_backed_ledger() -> None:
    """ADR-0013 end to end: the setting a real deployment sets produces a
    ledger that can actually sign, through the exact wiring path
    gateway/dependencies.py uses at startup."""
    with run_vault() as vault_url:
        container = build_container(
            Settings(
                signing_key_mode="vault-transit",
                vault_url=vault_url,
                vault_token=_ROOT_TOKEN,
                vault_transit_key_name=_KEY_NAME,
            )
        )

        assert isinstance(container.ledger, Ed25519ChainLedger)
        container.ledger.public_key().public_bytes_raw()  # does not raise


@pytest.mark.asyncio
async def test_doctor_check_reports_vault_key_fingerprint() -> None:
    with run_vault() as vault_url:
        check = await check_signing_key(
            Settings(
                signing_key_mode="vault-transit",
                vault_url=vault_url,
                vault_token=_ROOT_TOKEN,
                vault_transit_key_name=_KEY_NAME,
            )
        )

        assert check.ok
        assert _KEY_NAME in check.detail
        assert "pubkey=" in check.detail


@pytest.mark.asyncio
async def test_run_all_reports_an_unreachable_vault_without_raising() -> None:
    """check_signing_key() is allowed to raise (as it does here, on connection
    refused) — run_all() is the layer that converts that into a failed Check
    rather than crashing doctor/readyz."""
    settings = Settings(
        signing_key_mode="vault-transit",
        vault_url="http://127.0.0.1:1",
        vault_token="x",
        vault_transit_key_name="whatever",
    )

    checks = await run_all(settings)

    signing_key = next(c for c in checks if c.name == "signing_key")
    assert not signing_key.ok


def test_public_key_bytes_are_a_valid_base64_transit_response() -> None:
    """Guards the base64-decode path against Vault ever changing its
    response shape out from under an untested assumption."""
    with run_vault() as vault_url:
        response = httpx.get(
            f"{vault_url}/v1/transit/keys/{_KEY_NAME}", headers={"X-Vault-Token": _ROOT_TOKEN}
        )
        raw_field = response.json()["data"]["keys"]["1"]["public_key"]
        assert len(base64.b64decode(raw_field)) == 32
