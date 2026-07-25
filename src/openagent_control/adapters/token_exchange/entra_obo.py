"""Microsoft Entra ID On-Behalf-Of token exchange adapter.

Entra's OBO flow predates RFC 8693 and uses the jwt-bearer grant with
`requested_token_use=on_behalf_of`. The `audience` argument of the port maps to
Entra's `scope` parameter (callers typically pass `api://<app-id>/.default`).
See ADR-0004.
"""

from __future__ import annotations

import httpx

from openagent_control.domain.errors import TokenExchangeError

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class EntraOnBehalfOfTokenExchange:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def exchange(self, subject_token: str, audience: str) -> str:
        data = {
            "grant_type": _GRANT_TYPE,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "assertion": subject_token,
            "scope": audience,
            "requested_token_use": "on_behalf_of",
        }
        try:
            response = await self._client.post(self._token_url, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TokenExchangeError(str(exc)) from exc

        access_token = response.json().get("access_token")
        if not access_token:
            raise TokenExchangeError("Entra returned no access_token")
        return str(access_token)

    async def aclose(self) -> None:
        await self._client.aclose()
