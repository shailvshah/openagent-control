from __future__ import annotations

import httpx
import pytest

from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.domain.errors import TokenExchangeError


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_rfc8693_sends_token_exchange_grant_and_returns_token() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"access_token": "obo-token-123"})

    exchange = Rfc8693TokenExchange(
        token_url="https://idp.test/token",
        client_id="gateway",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    token = await exchange.exchange("subject-tok", "urn:my:audience")

    assert token == "obo-token-123"
    body = str(seen["body"])
    assert "token-exchange" in body
    assert "subject_token=subject-tok" in body
    assert "audience=urn%3Amy%3Aaudience" in body
    assert str(seen["auth"]).startswith("Basic ")  # client credentials


@pytest.mark.asyncio
async def test_rfc8693_idp_error_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    exchange = Rfc8693TokenExchange(
        token_url="https://idp.test/token",
        client_id="gateway",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    with pytest.raises(TokenExchangeError):
        await exchange.exchange("subject-tok", "aud")


@pytest.mark.asyncio
async def test_rfc8693_missing_access_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    exchange = Rfc8693TokenExchange(
        token_url="https://idp.test/token",
        client_id="gateway",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    with pytest.raises(TokenExchangeError, match="no access_token"):
        await exchange.exchange("subject-tok", "aud")


@pytest.mark.asyncio
async def test_entra_sends_obo_grant_and_returns_token() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "entra-obo-token"})

    exchange = EntraOnBehalfOfTokenExchange(
        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        client_id="app-id",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    token = await exchange.exchange("user-assertion", "api://target/.default")

    assert token == "entra-obo-token"
    body = str(seen["body"])
    assert "jwt-bearer" in body
    assert "requested_token_use=on_behalf_of" in body
    assert "assertion=user-assertion" in body


@pytest.mark.asyncio
async def test_entra_error_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    exchange = EntraOnBehalfOfTokenExchange(
        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        client_id="app-id",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    with pytest.raises(TokenExchangeError):
        await exchange.exchange("assertion", "scope")


@pytest.mark.asyncio
async def test_entra_missing_access_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    exchange = EntraOnBehalfOfTokenExchange(
        token_url="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        client_id="app-id",
        client_secret="secret",
        client=_client(httpx.MockTransport(handler)),
    )

    with pytest.raises(TokenExchangeError, match="no access_token"):
        await exchange.exchange("assertion", "scope")


@pytest.mark.asyncio
async def test_adapters_close_their_clients() -> None:
    await Rfc8693TokenExchange("https://idp.test/token", "id", "secret").aclose()
    await EntraOnBehalfOfTokenExchange("https://idp.test/token", "id", "secret").aclose()
