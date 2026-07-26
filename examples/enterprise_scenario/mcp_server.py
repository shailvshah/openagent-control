"""A real MCP server: JSON-RPC `tools/list` / `tools/call` over a real SQLite
database, protected by real OAuth 2.0 bearer-token validation.

This is the piece that makes the rest of the scenario honest. The repo's
docker-compose previously pointed the gateway at `hashicorp/http-echo`, which
returns a fixed string and never inspects the `Authorization` header -- so a
demo against it "proved" governance while an agent that skipped the gateway
entirely would have been served just the same.

This server instead:

  * validates the bearer token's RS256 signature against the authorization
    server's published JWKS,
  * requires `aud` to be this API's own identifier -- so the agent's own
    gateway-audience token is rejected here even though it is perfectly valid
    at the gateway (confused-deputy prevention),
  * requires the scope the specific tool needs,
  * and only then runs a real parameterised SQL query.

Because of that, removing the gateway from the path breaks the call. The
gateway is load-bearing, and `scenario.py` demonstrates exactly that.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from jwt import PyJWKClient

AUDIENCE = "https://finance-mcp.corp.net"

# Each tool declares the OAuth scope required to invoke it. A token that is
# valid but under-scoped is refused -- least privilege at the resource server,
# not only at the gateway.
TOOLS: dict[str, dict[str, Any]] = {
    "read_query": {
        "description": "Read invoice rows for a given quarter.",
        "scope": "invoices:read",
        "inputSchema": {
            "type": "object",
            "properties": {"quarter": {"type": "string"}},
            "required": ["quarter"],
        },
    },
    "update_record": {
        "description": "Update an invoice's status.",
        "scope": "invoices:write",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}, "status": {"type": "string"}},
            "required": ["invoice_id", "status"],
        },
    },
}

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
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE invoices SET status = ? WHERE invoice_id = ?", (status, invoice_id)
                )
        if cursor.rowcount == 0:
            raise KeyError(invoice_id)
        return {"invoice_id": invoice_id, "status": status, "updated": True}


class TokenValidationError(Exception):
    """Raised when the presented bearer token is missing, invalid, or under-scoped."""


class BearerTokenValidator:
    """Validates tokens against the authorization server's live JWKS."""

    def __init__(self, jwks_uri: str, issuer: str, audience: str = AUDIENCE) -> None:
        self._jwks_client = PyJWKClient(jwks_uri)
        self._issuer = issuer
        self._audience = audience

    def validate(self, authorization_header: str, required_scope: str) -> dict[str, Any]:
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise TokenValidationError("missing bearer token")

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
        except jwt.InvalidTokenError as exc:
            raise TokenValidationError(str(exc)) from exc

        granted = str(claims.get("scope", "")).split()
        if required_scope not in granted:
            raise TokenValidationError(
                f"token lacks required scope '{required_scope}' (has: {granted or 'none'})"
            )
        return claims


def _make_handler(
    store: InvoiceStore, validator: BearerTokenValidator
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status: int, request_id: Any, code: int, message: str) -> None:
            self._respond(
                status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._error(400, None, -32700, "Parse error")
                return

            request_id = payload.get("id")
            method = payload.get("method")
            authorization = self.headers.get("Authorization", "")

            if method == "tools/list":
                try:
                    validator.validate(authorization, "invoices:read")
                except TokenValidationError as exc:
                    self._error(401, request_id, -32001, f"Unauthorized: {exc}")
                    return
                self._respond(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "tools": [
                                {"name": name, "description": spec["description"],
                                 "inputSchema": spec["inputSchema"]}
                                for name, spec in TOOLS.items()
                            ]
                        },
                    },
                )
                return

            if method != "tools/call":
                self._error(400, request_id, -32601, f"Method not found: {method}")
                return

            params = payload.get("params") or {}
            name = str(params.get("name") or "")
            spec = TOOLS.get(name)
            if spec is None:
                self._error(400, request_id, -32602, f"Unknown tool: {name}")
                return

            try:
                claims = validator.validate(authorization, spec["scope"])
            except TokenValidationError as exc:
                self._error(401, request_id, -32001, f"Unauthorized: {exc}")
                return

            arguments = params.get("arguments") or {}
            try:
                if name == "read_query":
                    result = store.read_query(str(arguments.get("quarter", "")))
                else:
                    result = store.update_record(
                        str(arguments["invoice_id"]), str(arguments["status"])
                    )
            except KeyError as exc:
                self._error(400, request_id, -32602, f"No such invoice: {exc}")
                return

            # Echo who the resource server believes it served, from the token's
            # own delegation claims -- not from anything the caller asserted.
            result["_served_for"] = claims.get("sub")
            result["_via_actor"] = (claims.get("act") or {}).get("sub")
            self._respond(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

        def log_message(self, *args: object) -> None:
            pass

    return Handler


def build_mcp_server(
    jwks_uri: str,
    issuer: str,
    host: str = "127.0.0.1",
    port: int = 0,
    audience: str = AUDIENCE,
) -> ThreadingHTTPServer:
    """Binds the socket and returns the server, not yet serving.

    `audience` is configurable because a real IdP decides the identifier: with
    Keycloak the downstream API's audience is its client id, not a URI we pick.
    """
    store = InvoiceStore()
    validator = BearerTokenValidator(jwks_uri, issuer, audience)
    return ThreadingHTTPServer((host, port), _make_handler(store, validator))


@contextmanager
def run_mcp_server(jwks_uri: str, issuer: str, audience: str = AUDIENCE) -> Iterator[str]:
    """Starts the MCP server; yields its URL."""
    http = build_mcp_server(jwks_uri, issuer, audience=audience)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
