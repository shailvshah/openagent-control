"""A real OAuth 2.0 authorization server: OIDC discovery, JWKS, and RFC 8693
token exchange.

This is not a stub that returns a fixed string. It generates an RSA key pair,
publishes a real JWKS, mints real RS256-signed JWTs, and implements the actual
RFC 8693 token-exchange grant -- authenticating the requesting client with HTTP
Basic, verifying the presented `subject_token`'s signature and expiry, and
minting a NEW token narrowly scoped to the requested `audience` with an `act`
(actor) claim recording that the gateway acted on the subject's behalf.

It stands in for an Okta org authorization server (or Entra ID) only in the sense
that it runs locally instead of in a tenant. The protocol, the crypto, and the
claims are the same ones a real deployment exchanges -- which is what makes the
downstream MCP server's token validation a real check rather than theatre.

See the `enterprise-idp-integration` skill for the Okta/Entra endpoint shapes
this mirrors, and ADR-0004 for how the gateway uses the exchanged token.
"""

from __future__ import annotations

import base64
import datetime
import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

KID = "oac-scenario-key-1"
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/keys"
TOKEN_PATH = "/oauth2/v1/token"

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
CLIENT_CREDENTIALS_GRANT = "client_credentials"

# The gateway authenticates to the token endpoint as a confidential client.
# In production these are the gateway's own registered client credentials.
GATEWAY_CLIENT_ID = "openagent-control-gateway"
GATEWAY_CLIENT_SECRET = "scenario-only-not-a-real-secret"

# The agent workload is a registered client in its own right -- that is how it
# obtains the access token it presents to the gateway, exactly as a service
# principal would against Okta or Entra. Secrets here are scenario-only.
AGENT_CLIENT_ID = "finance-invoice-svc"
AGENT_CLIENT_SECRET = "scenario-only-not-a-real-secret"

_REGISTERED_CLIENTS = {
    GATEWAY_CLIENT_ID: GATEWAY_CLIENT_SECRET,
    AGENT_CLIENT_ID: AGENT_CLIENT_SECRET,
}

# Only the gateway may perform token exchange. An agent that could exchange
# tokens itself would be able to mint its own downstream credentials, which is
# precisely what the control plane exists to prevent.
_EXCHANGE_CLIENTS = {GATEWAY_CLIENT_ID}

# How long a brokered downstream credential lives. Deliberately short: the whole
# point of brokering is that a leaked credential expires before it is useful.
BROKERED_TOKEN_TTL_SECONDS = 300

# Which scopes the authorization server is willing to mint for a given audience.
# A real deployment reads this from the API's registered scope list.
_AUDIENCE_SCOPES = {
    "https://finance-mcp.corp.net": "invoices:read invoices:write",
}


class AuthorizationServer:
    """Mints and verifies tokens. The HTTP handler below is a thin shell over this."""

    def __init__(self, issuer: str, private_key: rsa.RSAPrivateKey) -> None:
        self.issuer = issuer
        self.discovery_url = issuer + DISCOVERY_PATH
        self.token_url = issuer + TOKEN_PATH
        self._private_key = private_key
        self._public_key = private_key.public_key()

    # --- minting -------------------------------------------------------

    def mint(self, audience: str, claims: dict[str, Any], ttl_seconds: int = 3600) -> str:
        now = datetime.datetime.now(datetime.UTC)
        payload = {
            "iss": self.issuer,
            "aud": audience,
            "iat": now,
            "exp": now + datetime.timedelta(seconds=ttl_seconds),
            **claims,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256", headers={"kid": KID})

    def mint_agent_token(self, gateway_audience: str, client_id: str, sponsor: str | None) -> str:
        """The access token the agent workload presents to the gateway.

        Shaped like an Entra ID delegated (OBO-capable) token when `sponsor` is
        set -- `azp` identifies the calling workload, `sub` the human it acts
        for -- and like a client-credentials token when it is not.
        """
        claims: dict[str, Any] = {"azp": client_id}
        if sponsor:
            claims["sub"] = sponsor
        return self.mint(gateway_audience, claims)

    def mint_sponsor_token(self, gateway_audience: str, sponsor: str) -> str:
        """The human sponsor's own token, presented as the RFC 8693 subject_token."""
        return self.mint(gateway_audience, {"sub": sponsor, "scope": "invoices:read"})

    # --- verifying -----------------------------------------------------

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=self._public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=self.issuer,
            options={"require": ["iss", "aud", "exp"]},
        )
        return claims

    # --- RFC 8693 ------------------------------------------------------

    def token(
        self, form: dict[str, str], client_id: str, gateway_audience: str
    ) -> tuple[int, dict[str, Any]]:
        """Dispatches the token endpoint's supported grants."""
        grant = form.get("grant_type")
        if grant == CLIENT_CREDENTIALS_GRANT:
            return self.client_credentials(client_id, gateway_audience)
        if grant == TOKEN_EXCHANGE_GRANT:
            if client_id not in _EXCHANGE_CLIENTS:
                return 403, {
                    "error": "unauthorized_client",
                    "error_description": "client is not permitted to perform token exchange",
                }
            return self.exchange(form, gateway_audience)
        return 400, {"error": "unsupported_grant_type"}

    def client_credentials(self, client_id: str, audience: str) -> tuple[int, dict[str, Any]]:
        """How a workload obtains its own access token for the gateway.

        No `sub`: this is the agent acting autonomously, as itself. A delegated
        (human-sponsored) token comes from an interactive flow instead.
        """
        access_token = self.mint(audience, {"azp": client_id})
        return 200, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def exchange(self, form: dict[str, str], gateway_audience: str) -> tuple[int, dict[str, Any]]:
        """Implements the token-exchange grant. Returns (status, body)."""
        audience = form.get("audience", "")
        if audience not in _AUDIENCE_SCOPES:
            return 400, {"error": "invalid_target", "error_description": f"unknown audience {audience}"}

        subject_token = form.get("subject_token", "")
        try:
            # The subject token was issued by us, for the gateway. Verifying it
            # here is what stops the gateway from exchanging an arbitrary
            # attacker-supplied string for a real downstream credential.
            subject_claims = self.verify(subject_token, gateway_audience)
        except jwt.InvalidTokenError as exc:
            return 400, {"error": "invalid_grant", "error_description": str(exc)}

        # A delegated subject token carries `sub` (the human); an autonomous
        # agent's own token carries only `azp` (the workload). Either is a valid
        # subject for exchange -- the downstream sees who it is acting for.
        subject = subject_claims.get("sub") or subject_claims.get("azp")
        if not subject:
            return 400, {
                "error": "invalid_grant",
                "error_description": "subject_token identifies no subject (no sub or azp claim)",
            }

        access_token = self.mint(
            audience,
            {
                "sub": subject,
                "scope": _AUDIENCE_SCOPES[audience],
                # RFC 8693 section 4.1: records the delegation chain -- the
                # downstream service can see the gateway acted for this subject.
                "act": {"sub": GATEWAY_CLIENT_ID},
            },
            ttl_seconds=BROKERED_TOKEN_TTL_SECONDS,
        )
        return 200, {
            "access_token": access_token,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer",
            "expires_in": BROKERED_TOKEN_TTL_SECONDS,
            "scope": _AUDIENCE_SCOPES[audience],
        }


def _authenticate_client(header: str) -> str | None:
    """Returns the authenticated client_id, or None. HTTP Basic, per RFC 6749."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    client_id, _, client_secret = decoded.partition(":")
    if _REGISTERED_CLIENTS.get(client_id) != client_secret:
        return None
    return client_id


def _make_handler(
    context: dict[str, Any], gateway_audience: str
) -> type[BaseHTTPRequestHandler]:
    """Handler bound to a context dict that is populated after the socket binds.

    The issuer URL must contain the assigned port, which is only known after
    binding -- and both the gateway and the MCP server validate `iss`, so it
    cannot be a placeholder. Requests cannot arrive before `serve_forever`
    starts, by which point the context is filled in.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @property
        def auth_server(self) -> AuthorizationServer:
            server: AuthorizationServer = context["server"]
            return server

        @property
        def jwks_body(self) -> bytes:
            body: bytes = context["jwks_body"]
            return body

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            auth = self.auth_server
            if self.path == DISCOVERY_PATH:
                self._respond(
                    200,
                    {
                        "issuer": auth.issuer,
                        "jwks_uri": auth.issuer + JWKS_PATH,
                        "token_endpoint": auth.token_url,
                        "grant_types_supported": [
                            CLIENT_CREDENTIALS_GRANT,
                            TOKEN_EXCHANGE_GRANT,
                        ],
                        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                    },
                )
                return
            if self.path == JWKS_PATH:
                body = self.jwks_body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._respond(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != TOKEN_PATH:
                self._respond(404, {"error": "not_found"})
                return
            client_id = _authenticate_client(self.headers.get("Authorization", ""))
            if client_id is None:
                self._respond(401, {"error": "invalid_client"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            status, body = self.auth_server.token(form, client_id, gateway_audience)
            self._respond(status, body)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


def build_authorization_server(
    gateway_audience: str,
    host: str = "127.0.0.1",
    port: int = 0,
    issuer: str | None = None,
) -> tuple[ThreadingHTTPServer, AuthorizationServer]:
    """Binds the socket and returns the (http, auth) pair, not yet serving.

    `issuer` overrides the derived URL for deployments where the address
    clients reach differs from the bind address (e.g. a container hostname).
    It must match what clients use, because `iss` is validated.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})

    context: dict[str, Any] = {}
    http = ThreadingHTTPServer((host, port), _make_handler(context, gateway_audience))

    auth_server = AuthorizationServer(
        issuer or f"http://{host}:{http.server_port}", private_key
    )
    context["server"] = auth_server
    context["jwks_body"] = json.dumps({"keys": [jwk]}).encode()
    return http, auth_server


@contextmanager
def run_authorization_server(gateway_audience: str) -> Iterator[AuthorizationServer]:
    http, auth_server = build_authorization_server(gateway_audience)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield auth_server
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
