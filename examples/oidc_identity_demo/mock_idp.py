"""A tiny in-process mock OIDC IdP: serves a discovery document and JWKS, and
mints RS256 access tokens shaped like real Okta / Entra ID tokens.

Runs on a random localhost port in a background thread so the demo needs no
external services (no real Okta org, no Entra tenant, no docker) -- see the
enterprise-idp-integration skill and ADR-0010 for what these tokens' claims
mean and how OidcJwksIdentityProvider validates them.
"""

from __future__ import annotations

import datetime
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

_KID = "demo-key-1"
_DISCOVERY_PATH = "/.well-known/openid-configuration"
_JWKS_PATH = "/keys"


class MockIdp:
    def __init__(self, base_url: str, private_key: rsa.RSAPrivateKey) -> None:
        self.base_url = base_url
        self.discovery_url = base_url + _DISCOVERY_PATH
        self.issuer = base_url
        self._private_key = private_key

    def mint_token(self, audience: str, claims: dict[str, object], expires_in: int = 300) -> str:
        now = datetime.datetime.now(datetime.UTC)
        payload = {
            "iss": self.issuer,
            "aud": audience,
            "exp": now + datetime.timedelta(seconds=expires_in),
            **claims,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256", headers={"kid": _KID})


def _make_handler(jwks_body: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = f"http://{self.headers['Host']}"
            if self.path == _DISCOVERY_PATH:
                body = json.dumps({"issuer": base, "jwks_uri": base + _JWKS_PATH}).encode()
            elif self.path == _JWKS_PATH:
                body = jwks_body
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@contextmanager
def run_mock_idp() -> Iterator[MockIdp]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    jwks_body = json.dumps({"keys": [jwk]}).encode()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(jwks_body))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield MockIdp(f"http://127.0.0.1:{server.server_port}", private_key)
    finally:
        server.shutdown()
        thread.join()
