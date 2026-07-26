"""Wiring tests: settings select the right adapter behind each port."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.mcp_upstream.streamable_http import StreamableHttpMCPUpstream
from openagent_control.adapters.registry.caching import CachingAgentRegistry
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.adapters.token_exchange.caching import CachingTokenExchange
from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.config import Settings
from openagent_control.gateway.dependencies import build_container


def test_default_wiring_uses_header_identity_and_stub_exchange() -> None:
    container = build_container(Settings())

    assert isinstance(container.identity_provider, HeaderIdentityProvider)
    assert isinstance(container.token_exchange, StubTokenExchange)
    assert isinstance(container.agent_registry, FileAgentRegistry)
    assert isinstance(container.ledger, Ed25519ChainLedger)
    assert container.db_engine is None
    assert container.redis_client is None


class _DiscoveryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        base = f"http://{self.headers['Host']}"
        body = json.dumps({"issuer": base, "jwks_uri": base + "/keys"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def discovery_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DiscoveryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/.well-known/openid-configuration"
    finally:
        server.shutdown()
        thread.join()


def test_oidc_jwks_mode_wires_oidc_identity_provider(discovery_server: str) -> None:
    container = build_container(
        Settings(
            identity_mode="oidc-jwks", oidc_discovery_url=discovery_server, oidc_audience="oac"
        )
    )

    assert isinstance(container.identity_provider, OidcJwksIdentityProvider)


def test_database_url_wires_postgres_registry_and_ledger() -> None:
    container = build_container(Settings(database_url="sqlite+aiosqlite:///:memory:"))

    assert isinstance(container.agent_registry, PostgresAgentRegistry)
    assert isinstance(container.ledger, PostgresLedger)
    assert container.db_engine is not None


def test_redis_url_wraps_registry_and_token_exchange_with_caching() -> None:
    container = build_container(Settings(redis_url="redis://localhost:6379/0"))

    assert isinstance(container.agent_registry, CachingAgentRegistry)
    assert isinstance(container.token_exchange, CachingTokenExchange)
    assert container.redis_client is not None
    kwargs = container.redis_client.connection_pool.connection_kwargs
    assert kwargs.get("decode_responses") is True


def test_database_url_without_persistence_extra_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "openagent_control.adapters.db.session", None)

    with pytest.raises(RuntimeError, match="persistence"):
        build_container(Settings(database_url="sqlite+aiosqlite:///:memory:"))


def test_redis_url_without_persistence_extra_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "openagent_control.adapters.registry.caching", None)

    with pytest.raises(RuntimeError, match="persistence"):
        build_container(Settings(redis_url="redis://localhost:6379/0"))


@pytest.mark.asyncio
async def test_aclose_disposes_db_engine_and_redis_client() -> None:
    container = build_container(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            redis_url="redis://localhost:6379/0",
        )
    )

    await container.aclose()  # must not raise; no real connections were opened


def test_jwt_svid_mode_wires_jwt_identity_provider(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "bundle.pem"
    pem_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    container = build_container(
        Settings(identity_mode="jwt-svid", jwt_svid_public_key_path=str(pem_path))
    )

    assert isinstance(container.identity_provider, JwtSvidIdentityProvider)


def test_rfc8693_mode_wires_okta_compatible_exchange() -> None:
    container = build_container(
        Settings(
            token_exchange_mode="rfc8693",
            token_exchange_url="https://idp.test/token",
            token_exchange_client_id="gateway",
            token_exchange_client_secret="secret",
        )
    )

    assert isinstance(container.token_exchange, Rfc8693TokenExchange)


def test_entra_mode_wires_obo_exchange() -> None:
    container = build_container(
        Settings(
            token_exchange_mode="entra",
            token_exchange_url="https://login.microsoftonline.com/t/oauth2/v2.0/token",
            token_exchange_client_id="app",
            token_exchange_client_secret="secret",
        )
    )

    assert isinstance(container.token_exchange, EntraOnBehalfOfTokenExchange)


def test_default_upstream_mode_speaks_real_mcp_transport() -> None:
    """A real MCP server answers a bare JSON-RPC POST with 406, so the
    Streamable HTTP adapter must be the default (ADR-0011)."""
    container = build_container(Settings(mcp_upstream_url="http://upstream/mcp"))

    assert isinstance(container.mcp_upstream, StreamableHttpMCPUpstream)


def test_raw_jsonrpc_mode_selects_the_plain_http_adapter() -> None:
    container = build_container(
        Settings(mcp_upstream_url="http://upstream", mcp_upstream_mode="raw-jsonrpc")
    )

    assert isinstance(container.mcp_upstream, HttpMCPUpstream)
