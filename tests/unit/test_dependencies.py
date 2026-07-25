"""Wiring tests: settings select the right adapter behind each port."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.registry.caching import CachingAgentRegistry
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.adapters.token_exchange.caching import CachingTokenExchange
from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.config import Settings
from openagent_control.gateway.dependencies import build_container


def test_default_wiring_uses_header_identity_and_stub_exchange() -> None:
    container = build_container(Settings())

    assert isinstance(container.identity_provider, HeaderIdentityProvider)
    assert isinstance(container.token_exchange, StubTokenExchange)
    assert isinstance(container.agent_registry, FileAgentRegistry)
    assert isinstance(container.ledger, Ed25519ChainLedger)
    assert container.db_engine is None
    assert container.redis_client is None


def test_database_url_wires_postgres_registry_and_ledger() -> None:
    container = build_container(Settings(database_url="sqlite+aiosqlite:///:memory:"))

    assert isinstance(container.agent_registry, PostgresAgentRegistry)
    assert isinstance(container.ledger, PostgresLedger)
    assert container.db_engine is not None


def test_redis_url_wraps_registry_and_token_exchange_with_caching() -> None:
    container = build_container(Settings(redis_url="redis://localhost:6379/0"))

    assert isinstance(container.agent_registry, CachingAgentRegistry)
    assert isinstance(container.token_exchange, CachingTokenExchange)
    assert container.redis_client is not None
    kwargs = container.redis_client.connection_pool.connection_kwargs
    assert kwargs.get("decode_responses") is True


def test_database_url_without_persistence_extra_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "openagent_control.adapters.db.session", None)

    with pytest.raises(RuntimeError, match="persistence"):
        build_container(Settings(database_url="sqlite+aiosqlite:///:memory:"))


def test_redis_url_without_persistence_extra_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "openagent_control.adapters.registry.caching", None)

    with pytest.raises(RuntimeError, match="persistence"):
        build_container(Settings(redis_url="redis://localhost:6379/0"))


@pytest.mark.asyncio
async def test_aclose_disposes_db_engine_and_redis_client() -> None:
    container = build_container(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            redis_url="redis://localhost:6379/0",
        )
    )

    await container.aclose()  # must not raise; no real connections were opened


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
