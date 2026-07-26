"""Process harness for the enterprise scenario: real OPA, the real gateway, and
the scenario's registry file.

Kept separate from `scenario.py` so the integration tests can stand up the same
stack without importing the LangGraph agent (and therefore without requiring the
optional `examples` dependency group).
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import uvicorn
import yaml

from examples.enterprise_scenario.authorization_server import AGENT_CLIENT_ID
from openagent_control.config import Settings
from openagent_control.gateway.app import create_app

__all__ = [
    "AGENT_CLIENT_ID",
    "GATEWAY_AUDIENCE",
    "HUMAN_SPONSOR",
    "REPO_ROOT",
    "build_settings",
    "free_port",
    "run_gateway",
    "run_opa",
    "wait_for",
    "write_registry",
]

GATEWAY_AUDIENCE = "api://openagent-control-gateway"
HUMAN_SPONSOR = "dana.reed@corp.net"
REPO_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def wait_for(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


@contextlib.contextmanager
def run_opa() -> Iterator[str]:
    """Starts a real OPA server over the repo's real Rego policy."""
    if shutil.which("opa") is None:
        raise RuntimeError(
            "the `opa` binary is required for this scenario (the policy engine is "
            "real, not simulated). Install it with: brew install opa"
        )
    port = free_port()
    process = subprocess.Popen(
        ["opa", "run", "--server", "--addr", f"127.0.0.1:{port}", str(REPO_ROOT / "policies")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}/v1/data/openagent/authz"
    finally:
        process.terminate()
        process.wait(timeout=10)


@contextlib.contextmanager
def run_gateway(settings: Settings) -> Iterator[str]:
    """Runs the real gateway app under uvicorn on a real port.

    Deliberately not TestClient: the agent must make real network calls for the
    gateway-bypass comparison to mean anything.
    """
    port = free_port()
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for(f"http://127.0.0.1:{port}/healthz")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def write_registry(path: Path, issuer: str, status: str = "active") -> None:
    """The Master Agent Registry entry for this scenario's agent (ADR-0008).

    Written at runtime only because the local authorization server's issuer URL
    contains a dynamically assigned port; in a real deployment this is a
    git-reviewed file (or a row in oac.agents).
    """
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "spiffe_id": f"oidc://{issuer}/{AGENT_CLIENT_ID}",
                        "display_name": "Finance Invoice Service",
                        "purpose": "Read and reconcile finance invoices.",
                        "owner": "alice@corp.net",
                        "risk_tier": "medium",
                        "status": status,
                        # read_query only. update_record is deliberately NOT
                        # granted -- that is what the policy-denial case exercises.
                        "granted_tools": ["read_query"],
                    }
                ]
            }
        )
    )


def build_settings(
    auth_discovery_url: str,
    auth_token_url: str,
    opa_url: str,
    mcp_url: str,
    registry_path: Path,
    delegated_audience: str,
    client_id: str,
    client_secret: str,
) -> Settings:
    """The same Settings a production deployment would set as OAC_* env vars."""
    return Settings(
        opa_url=opa_url,
        mcp_upstream_url=mcp_url,
        registry_path=str(registry_path),
        identity_mode="oidc-jwks",
        oidc_discovery_url=auth_discovery_url,
        oidc_audience=GATEWAY_AUDIENCE,
        token_exchange_mode="rfc8693",
        token_exchange_url=auth_token_url,
        token_exchange_client_id=client_id,
        token_exchange_client_secret=client_secret,
        delegated_audience=delegated_audience,
    )
