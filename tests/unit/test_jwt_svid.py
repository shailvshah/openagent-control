from __future__ import annotations

import datetime
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.domain.errors import IdentityError

_AUDIENCE = "openagent-control"
_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


@pytest.fixture(scope="module")
def keypair() -> tuple[rsa.RSAPrivateKey, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem


@pytest.fixture
def provider(keypair: tuple[rsa.RSAPrivateKey, bytes], tmp_path: Path) -> JwtSvidIdentityProvider:
    key_path = tmp_path / "trust-bundle.pem"
    key_path.write_bytes(keypair[1])
    return JwtSvidIdentityProvider(public_key_path=key_path, audience=_AUDIENCE)


def _mint(
    private_key: rsa.RSAPrivateKey,
    sub: str = _SPIFFE_ID,
    audience: str = _AUDIENCE,
    expires_in: int = 300,
) -> str:
    now = datetime.datetime.now(datetime.UTC)
    return jwt.encode(
        {"sub": sub, "aud": audience, "exp": now + datetime.timedelta(seconds=expires_in)},
        private_key,
        algorithm="RS256",
    )


@pytest.mark.asyncio
async def test_valid_svid_yields_identity_and_sponsor(
    provider: JwtSvidIdentityProvider, keypair: tuple[rsa.RSAPrivateKey, bytes]
) -> None:
    token = _mint(keypair[0])

    agent = await provider.identify(
        {"Authorization": f"Bearer {token}", "X-Human-Sponsor": "alice@corp.net"}
    )

    assert agent.spiffe_id == _SPIFFE_ID
    assert agent.human_sponsor == "alice@corp.net"


def test_unsupported_key_type_fails_at_startup(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    pem = (
        X25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    key_path = tmp_path / "bad-bundle.pem"
    key_path.write_bytes(pem)

    with pytest.raises(ValueError, match="unsupported trust-bundle key type"):
        JwtSvidIdentityProvider(public_key_path=key_path, audience=_AUDIENCE)


@pytest.mark.asyncio
async def test_missing_bearer_header_is_rejected(provider: JwtSvidIdentityProvider) -> None:
    with pytest.raises(IdentityError, match="Bearer"):
        await provider.identify({})


@pytest.mark.asyncio
async def test_expired_svid_is_rejected(
    provider: JwtSvidIdentityProvider, keypair: tuple[rsa.RSAPrivateKey, bytes]
) -> None:
    token = _mint(keypair[0], expires_in=-60)

    with pytest.raises(IdentityError, match="invalid JWT-SVID"):
        await provider.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(
    provider: JwtSvidIdentityProvider, keypair: tuple[rsa.RSAPrivateKey, bytes]
) -> None:
    token = _mint(keypair[0], audience="some-other-system")

    with pytest.raises(IdentityError, match="invalid JWT-SVID"):
        await provider.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_wrong_signing_key_is_rejected(provider: JwtSvidIdentityProvider) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _mint(other_key)

    with pytest.raises(IdentityError, match="invalid JWT-SVID"):
        await provider.identify({"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_non_spiffe_subject_is_rejected(
    provider: JwtSvidIdentityProvider, keypair: tuple[rsa.RSAPrivateKey, bytes]
) -> None:
    token = _mint(keypair[0], sub="user@corp.net")

    with pytest.raises(IdentityError, match="not a SPIFFE ID"):
        await provider.identify({"Authorization": f"Bearer {token}"})
