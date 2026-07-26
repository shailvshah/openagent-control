"""A second real MCP server, so the multi-upstream path (ADR-0016) has two
genuinely different downstream systems to route between.

Real enterprises do not put every tool on one MCP server — finance tools live
next to the finance data, CRM tools next to the CRM. That is the whole reason
`RoutingMCPUpstream` exists, and proving it against two copies of the same
server would prove only the tool-name-collision path, never the merge.

Deliberately shares `mcp_server`'s `JwksTokenVerifier` and audience: one
brokered credential is valid at both servers, which is what a single
`OAC_DELEGATED_AUDIENCE` for a fleet looks like. Everything else — transport,
OAuth resource-server protection, tool surface — is its own.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from examples.enterprise_scenario.harness import free_port
from examples.enterprise_scenario.mcp_server import AUDIENCE, REQUIRED_SCOPE, JwksTokenVerifier

_ACCOUNTS = {
    "ACME Corp": {"account_id": "ACC-1", "tier": "enterprise", "credit_limit": 50_000.0},
    "Globex": {"account_id": "ACC-2", "tier": "mid-market", "credit_limit": 15_000.0},
}


def build_server(
    jwks_uri: str,
    issuer: str,
    audience: str = AUDIENCE,
    host: str = "127.0.0.1",
    port: int = 0,
) -> FastMCP:
    server = FastMCP(
        "crm-mcp",
        host=host,
        port=port,
        token_verifier=JwksTokenVerifier(jwks_uri, issuer, audience),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(f"http://{host}:{port}"),
            required_scopes=[REQUIRED_SCOPE],
        ),
    )

    @server.tool()
    def lookup_account(customer: str) -> dict[str, Any]:
        """Look up a customer's CRM account record, e.g. customer 'ACME Corp'."""
        account = _ACCOUNTS.get(customer)
        if account is None:
            return {"found": False, "customer": customer}
        return {"found": True, "customer": customer, **account}

    return server


@contextlib.contextmanager
def run_crm_server(jwks_uri: str, issuer: str, audience: str = AUDIENCE) -> Iterator[str]:
    """Starts the CRM MCP server; yields its Streamable HTTP endpoint URL."""
    port = free_port()
    server = build_server(jwks_uri, issuer, audience, port=port)
    config = uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    http = uvicorn.Server(config)
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.post(url, timeout=1.0).status_code < 500:
                break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"CRM MCP server did not start on {url}")

    try:
        yield url
    finally:
        http.should_exit = True
        thread.join(timeout=10)
