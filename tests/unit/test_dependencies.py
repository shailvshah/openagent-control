"""Wiring tests: settings select the right adapter behind each port."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.config import Settings
from openagent_control.gateway.dependencies import build_container


def test_default_wiring_uses_header_identity_and_stub_exchange() -> None:
    container = build_container(Settings())

    assert isinstance(container.identity_provider, HeaderIdentityProvider)
    assert isinstance(container.token_exchange, StubTokenExchange)


def test_jwt_svid_mode_wires_jwt_identity_provider(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "bundle.pem"
    pem_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    container = build_container(
        Settings(identity_mode="jwt-svid", jwt_svid_public_key_path=str(pem_path))
    )

    assert isinstance(container.identity_provider, JwtSvidIdentityProvider)


def test_rfc8693_mode_wires_okta_compatible_exchange() -> None:
    container = build_container(
        Settings(
            token_exchange_mode="rfc8693",
            token_exchange_url="https://idp.test/token",
            token_exchange_client_id="gateway",
            token_exchange_client_secret="secret",
        )
    )

    assert isinstance(container.token_exchange, Rfc8693TokenExchange)


def test_entra_mode_wires_obo_exchange() -> None:
    container = build_container(
        Settings(
            token_exchange_mode="entra",
            token_exchange_url="https://login.microsoftonline.com/t/oauth2/v2.0/token",
            token_exchange_client_id="app",
            token_exchange_client_secret="secret",
        )
    )

    assert isinstance(container.token_exchange, EntraOnBehalfOfTokenExchange)
