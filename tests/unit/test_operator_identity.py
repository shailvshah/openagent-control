"""Operator identity adapters (ADR-0014).

OidcOperatorAuth reuses the same real-local-HTTP-server test pattern as
test_oidc_jwks.py (PyJWKClient makes real HTTP requests internally, so a
MockTransport can't intercept it).
"""

from __future__ import annotations

import datetime
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from openagent_control.adapters.operator_identity.api_key import ApiKeyOperatorAuth
from openagent_control.adapters.operator_identity.oidc import (
    OidcOperatorAuth,
    _resolve_dotted_claim,
)
from openagent_control.domain.errors import IdentityError

_ISSUER_PATH = "/.well-known/openid-configuration"
_JWKS_PATH = "/keys"
_AUDIENCE = "oac-control-plane"
_KID = "test-key-1"


class _IdpFixture:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.discovery_url = base_url + _ISSUER_PATH
        self.issuer = base_url

    @property
    def audience(self) -> str:
        return _AUDIENCE


@pytest.fixture(scope="module")
def idp_server() -> Iterator[tuple[_IdpFixture, rsa.RSAPrivateKey]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    jwks_body = json.dumps({"keys": [jwk]}).encode()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(jwks_body))
    base_url = f"http://127.0.0.1:{server.server_port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _IdpFixture(base_url), private_key
    finally:
        server.shutdown()
        thread.join()


def _make_handler(jwks_body: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = f"http://{self.headers['Host']}"
            if self.path == _ISSUER_PATH:
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


def _token(
    private_key: rsa.RSAPrivateKey,
    idp: _IdpFixture,
    claims: dict[str, object],
    expires_in: int = 300,
) -> str:
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "iss": idp.issuer,
        "aud": idp.audience,
        "exp": now + datetime.timedelta(seconds=expires_in),
        **claims,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": _KID})


def test_resolve_dotted_claim_walks_nested_dicts() -> None:
    claims: dict[str, object] = {"realm_access": {"roles": ["oac-operator", "other"]}}

    assert _resolve_dotted_claim(claims, "realm_access.roles") == ["oac-operator", "other"]
    assert _resolve_dotted_claim(claims, "realm_access.missing") is None
    assert _resolve_dotted_claim(claims, "missing.roles") is None


@pytest.mark.asyncio
async def test_top_level_array_role_claim_grants_access(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    """Entra's app-roles shape: a flat array at a top-level claim."""
    idp, key = idp_server
    auth = OidcOperatorAuth(
        idp.discovery_url, audience=idp.audience, role_claim="roles", required_role="oac-operator"
    )
    token = _token(key, idp, {"preferred_username": "alice@corp.net", "roles": ["oac-operator"]})

    subject = await auth.identify({"Authorization": f"Bearer {token}"})

    assert subject == "alice@corp.net"


@pytest.mark.asyncio
async def test_nested_dotted_role_claim_grants_access(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    """Keycloak's shape: realm roles nested under realm_access.roles."""
    idp, key = idp_server
    auth = OidcOperatorAuth(
        idp.discovery_url,
        audience=idp.audience,
        role_claim="realm_access.roles",
        required_role="oac-operator",
    )
    token = _token(
        key,
        idp,
        {"sub": "alice-uuid", "realm_access": {"roles": ["oac-operator", "other-role"]}},
    )

    subject = await auth.identify({"Authorization": f"Bearer {token}"})

    assert subject == "alice-uuid"


@pytest.mark.asyncio
async def test_missing_required_role_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    auth = OidcOperatorAuth(
        idp.discovery_url, audience=idp.audience, role_claim="roles", required_role="oac-operator"
    )
    token = _token(key, idp, {"preferred_username": "bob@corp.net", "roles": ["read-only"]})

    with pytest.raises(IdentityError, match="lacks required role"):
        await auth.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_missing_role_claim_entirely_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    auth = OidcOperatorAuth(
        idp.discovery_url, audience=idp.audience, role_claim="roles", required_role="oac-operator"
    )
    token = _token(key, idp, {"preferred_username": "bob@corp.net"})

    with pytest.raises(IdentityError, match="lacks required role"):
        await auth.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_missing_bearer_token_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, _key = idp_server
    auth = OidcOperatorAuth(
        idp.discovery_url, audience=idp.audience, role_claim="roles", required_role="oac-operator"
    )

    with pytest.raises(IdentityError, match="missing 'Authorization"):
        await auth.identify({})


def test_api_key_auth_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="OAC_CONTROL_PLANE_API_KEY"):
        ApiKeyOperatorAuth("")


@pytest.mark.asyncio
async def test_api_key_auth_accepts_the_configured_key() -> None:
    auth = ApiKeyOperatorAuth("secret-key")

    subject = await auth.identify({"Authorization": "Bearer secret-key"})

    assert subject == "api-key"


@pytest.mark.asyncio
async def test_api_key_auth_rejects_the_wrong_key() -> None:
    auth = ApiKeyOperatorAuth("secret-key")

    with pytest.raises(IdentityError, match="invalid control-plane API key"):
        await auth.identify({"Authorization": "Bearer wrong-key"})


@pytest.mark.asyncio
async def test_api_key_auth_rejects_a_missing_header() -> None:
    auth = ApiKeyOperatorAuth("secret-key")

    with pytest.raises(IdentityError, match="missing 'Authorization"):
        await auth.identify({})
