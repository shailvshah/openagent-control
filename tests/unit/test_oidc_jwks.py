"""OidcJwksIdentityProvider against a real local HTTP server serving a
discovery document and JWKS (PyJWKClient makes real urllib requests
internally, so a MockTransport can't intercept it — see ADR-0010)."""

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

from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.domain.errors import IdentityError

_ISSUER_PATH = "/.well-known/openid-configuration"
_JWKS_PATH = "/keys"
_AUDIENCE = "oac-gateway"
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


@pytest.mark.asyncio
async def test_client_credentials_token_uses_azp_as_identity(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)
    token = _token(key, idp, {"azp": "agent-client-id"})

    agent = await provider.identify({"Authorization": f"Bearer {token}"})

    assert agent.spiffe_id == f"oidc://{idp.issuer}/agent-client-id"
    assert agent.human_sponsor is None


@pytest.mark.asyncio
async def test_okta_style_cid_claim_is_used_when_azp_absent(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)
    token = _token(key, idp, {"cid": "okta-client-id", "sub": "okta-client-id"})

    agent = await provider.identify({"Authorization": f"Bearer {token}"})

    assert agent.spiffe_id == f"oidc://{idp.issuer}/okta-client-id"
    assert agent.human_sponsor is None  # sub == client id -> not a delegated user


@pytest.mark.asyncio
async def test_delegated_token_surfaces_sub_as_human_sponsor(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)
    token = _token(key, idp, {"azp": "agent-client-id", "sub": "alice@corp.net"})

    agent = await provider.identify({"Authorization": f"Bearer {token}"})

    assert agent.spiffe_id == f"oidc://{idp.issuer}/agent-client-id"
    assert agent.human_sponsor == "alice@corp.net"


@pytest.mark.asyncio
async def test_missing_client_id_claims_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)
    token = _token(key, idp, {"sub": "alice@corp.net"})

    with pytest.raises(IdentityError, match="client-id claims"):
        await provider.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)
    now = datetime.datetime.now(datetime.UTC)
    token = jwt.encode(
        {
            "iss": idp.issuer,
            "aud": "some-other-app",
            "exp": now + datetime.timedelta(seconds=300),
            "azp": "agent-client-id",
        },
        key,
        algorithm="RS256",
        headers={"kid": _KID},
    )

    with pytest.raises(IdentityError, match="invalid OIDC access token"):
        await provider.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_missing_bearer_header_is_rejected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, _key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience)

    with pytest.raises(IdentityError, match="Bearer"):
        await provider.identify({})


@pytest.mark.asyncio
async def test_explicit_issuer_override_is_respected(
    idp_server: tuple[_IdpFixture, rsa.RSAPrivateKey],
) -> None:
    idp, key = idp_server
    provider = OidcJwksIdentityProvider(idp.discovery_url, audience=idp.audience, issuer=idp.issuer)
    token = _token(key, idp, {"azp": "agent-client-id"})

    agent = await provider.identify({"Authorization": f"Bearer {token}"})

    assert agent.spiffe_id == f"oidc://{idp.issuer}/agent-client-id"
