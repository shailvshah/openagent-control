"""Contract smoke tests: every v1 adapter must structurally satisfy its port.

Per ADR-0006, adapters are swappable behind runtime-checkable Protocols; this
catches an adapter drifting from its port signature (renamed/removed method)
before the gateway wiring does. No I/O happens here — engine/session-factory and
Redis client construction are lazy, so this stays a pure structural check.
"""

from __future__ import annotations

import httpx
import pytest
import redis.asyncio as redis_asyncio

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.ledger.signing import ReceiptSigner
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.adapters.registry.caching import CachingAgentRegistry
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.adapters.token_exchange.caching import CachingTokenExchange
from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.ports import (
    AgentRegistry,
    AuditExporter,
    IdentityProvider,
    Ledger,
    MCPUpstream,
    PolicyEngine,
    TokenExchange,
)

_session_factory = make_session_factory(make_engine("sqlite+aiosqlite:///:memory:"))
_redis = redis_asyncio.Redis()


def _mock_discovery_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"issuer": "https://idp.test", "jwks_uri": "https://idp.test/keys"},
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("adapter", "port"),
    [
        (OPAPolicyEngine(opa_url="http://opa.test"), PolicyEngine),
        (HeaderIdentityProvider(), IdentityProvider),
        (
            OidcJwksIdentityProvider(
                "https://idp.test/.well-known/openid-configuration",
                audience="oac",
                client=httpx.Client(transport=_mock_discovery_transport()),
            ),
            IdentityProvider,
        ),
        (Ed25519ChainLedger(), Ledger),
        (PostgresLedger(_session_factory, ReceiptSigner()), Ledger),
        (StubTokenExchange(), TokenExchange),
        (Rfc8693TokenExchange("http://idp.test", "id", "secret"), TokenExchange),
        (EntraOnBehalfOfTokenExchange("http://idp.test", "id", "secret"), TokenExchange),
        (CachingTokenExchange(StubTokenExchange(), _redis, 300, 30), TokenExchange),
        (FileAgentRegistry("registry/agents.yaml"), AgentRegistry),
        (PostgresAgentRegistry(_session_factory), AgentRegistry),
        (
            CachingAgentRegistry(FileAgentRegistry("registry/agents.yaml"), _redis, 30),
            AgentRegistry,
        ),
        (HttpMCPUpstream(upstream_url="http://upstream.test"), MCPUpstream),
        (StdoutAuditExporter(), AuditExporter),
    ],
)
def test_adapter_satisfies_port(adapter: object, port: type) -> None:
    assert isinstance(adapter, port)
