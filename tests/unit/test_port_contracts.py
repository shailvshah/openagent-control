"""Contract smoke tests: every v1 adapter must structurally satisfy its port.

Per ADR-0006, adapters are swappable behind runtime-checkable Protocols; this
catches an adapter drifting from its port signature (renamed/removed method)
before the gateway wiring does.
"""

from __future__ import annotations

import pytest

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.ports import (
    AuditExporter,
    IdentityProvider,
    Ledger,
    MCPUpstream,
    PolicyEngine,
    TokenExchange,
)


@pytest.mark.parametrize(
    ("adapter", "port"),
    [
        (OPAPolicyEngine(opa_url="http://opa.test"), PolicyEngine),
        (HeaderIdentityProvider(), IdentityProvider),
        (Ed25519ChainLedger(), Ledger),
        (StubTokenExchange(), TokenExchange),
        (HttpMCPUpstream(upstream_url="http://upstream.test"), MCPUpstream),
        (StdoutAuditExporter(), AuditExporter),
    ],
)
def test_adapter_satisfies_port(adapter: object, port: type) -> None:
    assert isinstance(adapter, port)
