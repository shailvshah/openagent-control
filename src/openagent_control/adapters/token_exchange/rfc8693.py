"""RFC 8693 OAuth 2.0 Token Exchange adapter (Okta-compatible).

Swaps the human sponsor's subject token for a short-lived, audience-scoped access
token. Works against any authorization server implementing RFC 8693 (Okta org
authorization servers, Keycloak, etc.). See ADR-0004.
"""

from __future__ import annotations

import httpx

from openagent_control.domain.errors import TokenExchangeError

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class Rfc8693TokenExchange:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_url = token_url
        self._auth = (client_id, client_secret)
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def exchange(self, subject_token: str, audience: str) -> str:
        data = {
            "grant_type": _GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": _ACCESS_TOKEN_TYPE,
            "requested_token_type": _ACCESS_TOKEN_TYPE,
            "audience": audience,
        }
        try:
            response = await self._client.post(self._token_url, data=data, auth=self._auth)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TokenExchangeError(str(exc)) from exc

        access_token = response.json().get("access_token")
        if not access_token:
            raise TokenExchangeError("authorization server returned no access_token")
        return str(access_token)

    async def aclose(self) -> None:
        await self._client.aclose()
