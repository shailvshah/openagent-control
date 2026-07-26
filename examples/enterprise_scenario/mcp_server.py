"""A real MCP server: the official MCP Python SDK, Streamable HTTP transport,
OAuth 2.0 resource-server protection, and a real SQLite database.

This is the piece that makes the rest of the scenario honest, in two ways.

*Transport.* An earlier version of this file accepted a bare JSON-RPC POST,
which is not MCP — and it passed its tests only because the gateway's upstream
adapter sent exactly that. Both were wrong in the same direction. It now runs
on `FastMCP` with `transport="streamable-http"`, so it enforces the real
protocol (initialize handshake, session ids, SSE framing) exactly as GitHub's
production MCP server does.

*Authorization.* `JwksTokenVerifier` implements the SDK's `TokenVerifier` port:
it validates the bearer token's RS256 signature against the authorization
server's live JWKS and requires `aud` to be this API's own identifier. That
audience check is what makes the gateway load-bearing — the agent's own token
is valid at the gateway and useless here. Per the MCP authorization spec
(2025-06-18), a server MUST reject tokens not issued for it, and token
passthrough is forbidden; the gateway satisfies this by brokering a new
audience-scoped token (ADR-0004) rather than relaying the agent's.

The SDK also serves RFC 9728 protected-resource metadata and the
`WWW-Authenticate` 401 challenge for us, both of which the spec requires.
"""

from __future__ import annotations

import contextlib
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import uvicorn
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

AUDIENCE = "https://finance-mcp.corp.net"
REQUIRED_SCOPE = "invoices:read"

_SEED_INVOICES = [
    ("INV-1001", "ACME Corp", "Q3", 48_500.00, "open"),
    ("INV-1002", "Globex", "Q3", 12_250.00, "paid"),
    ("INV-1003", "Initech", "Q3", 7_800.00, "open"),
    ("INV-1004", "ACME Corp", "Q2", 31_000.00, "paid"),
]


class InvoiceStore:
    """Real persistence. Shared in-memory SQLite, safe across handler threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                "CREATE TABLE invoices ("
                "  invoice_id TEXT PRIMARY KEY,"
                "  customer   TEXT NOT NULL,"
                "  quarter    TEXT NOT NULL,"
                "  amount     REAL NOT NULL,"
                "  status     TEXT NOT NULL)"
            )
            self._conn.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?, ?)", _SEED_INVOICES)

    def read_query(self, quarter: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT invoice_id, customer, amount, status FROM invoices WHERE quarter = ?",
                (quarter,),
            ).fetchall()
        return {"quarter": quarter, "rows": [dict(row) for row in rows]}

    def update_record(self, invoice_id: str, status: str) -> dict[str, Any]:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE invoices SET status = ? WHERE invoice_id = ?", (status, invoice_id)
            )
        if cursor.rowcount == 0:
            raise ValueError(f"no such invoice: {invoice_id}")
        return {"invoice_id": invoice_id, "status": status, "updated": True}


class JwksTokenVerifier(TokenVerifier):
    """Validates bearer tokens against the authorization server's live JWKS."""

    def __init__(self, jwks_uri: str, issuer: str, audience: str = AUDIENCE) -> None:
        self._jwks_client = PyJWKClient(jwks_uri)
        self._issuer = issuer
        self._audience = audience

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key=signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["iss", "aud", "exp"]},
            )
        except jwt.InvalidTokenError:
            # Returning None makes the SDK answer 401 with the spec-required
            # WWW-Authenticate challenge.
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("sub") or ""),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=str(claims.get("sub") or ""),
            claims=claims,
        )


def build_server(
    jwks_uri: str,
    issuer: str,
    audience: str = AUDIENCE,
    host: str = "127.0.0.1",
    port: int = 0,
) -> FastMCP:
    """A real MCP server over a real database, protected by real OAuth."""
    store = InvoiceStore()
    server = FastMCP(
        "finance-mcp",
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
    def read_query(quarter: str) -> dict[str, Any]:
        """Read invoice rows for a given quarter, e.g. 'Q3'."""
        result = store.read_query(quarter)
        # Report who the resource server believes it served, taken from the
        # verified token's own delegation claims -- never from anything the
        # caller asserted. `act.sub` is RFC 8693's actor claim: the gateway.
        token = get_access_token()
        claims = (token.claims if token else None) or {}
        result["_served_for"] = claims.get("sub")
        result["_via_actor"] = (claims.get("act") or {}).get("sub")
        return result

    @server.tool()
    def update_record(invoice_id: str, status: str) -> dict[str, Any]:
        """Update an invoice's status, e.g. invoice_id 'INV-1001', status 'paid'."""
        return store.update_record(invoice_id, status)

    return server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.contextmanager
def run_mcp_server(jwks_uri: str, issuer: str, audience: str = AUDIENCE) -> Iterator[str]:
    """Starts the MCP server; yields its Streamable HTTP endpoint URL.

    Runs the SDK's ASGI app under our own uvicorn.Server rather than
    FastMCP.run(), which blocks and offers no shutdown hook -- tests need to
    reclaim the port between modules.
    """
    port = _free_port()
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
            # 401 is the expected unauthenticated answer, and proves it is serving.
            if httpx.post(url, timeout=1.0).status_code < 500:
                break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"MCP server did not start on {url}")

    try:
        yield url
    finally:
        http.should_exit = True
        thread.join(timeout=10)
