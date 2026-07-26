"""Tests for the checks behind `doctor` and `/readyz`.

Uses a real local HTTP server rather than a mocked transport: these checks
exist to answer "is the dependency actually reachable", and a stubbed client
would assert only that our own stub responds.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from openagent_control import diagnostics
from openagent_control.config import Settings


def _make_server(status_code: int, body: dict[str, Any]) -> ThreadingHTTPServer:
    payload = json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.fixture
def healthy_server() -> Iterator[str]:
    server = _make_server(200, {"issuer": "https://idp.example.com"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
async def test_opa_check_passes_when_health_returns_200(healthy_server: str) -> None:
    settings = Settings(opa_url=f"{healthy_server}/v1/data/openagent/authz")

    check = await diagnostics.check_opa(settings)

    assert check.ok and "HTTP 200" in check.detail


@pytest.mark.asyncio
async def test_opa_check_fails_when_unreachable() -> None:
    settings = Settings(opa_url="http://127.0.0.1:1/v1/data/openagent/authz")

    checks = await diagnostics.run_all(settings)

    opa = next(c for c in checks if c.name == "opa")
    assert not opa.ok
    # run_all must report, not raise — a readiness probe that 500s tells the
    # operator nothing about which dependency is down.
    assert "Error" in opa.detail or "error" in opa.detail


@pytest.mark.asyncio
async def test_identity_check_skips_when_not_using_oidc() -> None:
    check = await diagnostics.check_identity(Settings(identity_mode="header"))

    assert check.ok and "no IdP to reach" in check.detail


@pytest.mark.asyncio
async def test_identity_check_fetches_discovery_in_oidc_mode(healthy_server: str) -> None:
    settings = Settings(
        identity_mode="oidc-jwks",
        oidc_discovery_url=f"{healthy_server}/.well-known/openid-configuration",
        oidc_audience="api://gw",
    )

    check = await diagnostics.check_identity(settings)

    assert check.ok and "idp.example.com" in check.detail


@pytest.mark.asyncio
async def test_unset_backends_are_reported_as_intentional() -> None:
    settings = Settings(database_url="", redis_url="")

    database = await diagnostics.check_database(settings)
    redis = await diagnostics.check_redis(settings)

    assert database.ok and "in-process" in database.detail
    assert redis.ok and "caching disabled" in redis.detail


@pytest.mark.asyncio
async def test_registry_check_counts_agents(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  - spiffe_id: spiffe://corp/a\n"
        "    display_name: A\n    purpose: p\n    owner: o@x\n"
        "    risk_tier: low\n    status: active\n    granted_tools: []\n"
    )

    check = await diagnostics.check_registry(Settings(registry_path=str(path)))

    assert check.ok and "1 agent(s)" in check.detail


@pytest.mark.asyncio
async def test_bundled_registry_is_flagged_as_denying_everything() -> None:
    check = await diagnostics.check_registry(Settings(registry_path=""))

    assert check.ok
    assert "every agent will be denied" in check.detail


@pytest.mark.asyncio
async def test_missing_registry_is_reported_not_raised() -> None:
    checks = await diagnostics.run_all(Settings(registry_path="/nope/agents.yaml"))

    registry = next(c for c in checks if c.name == "registry")
    assert not registry.ok and "does not exist" in registry.detail
