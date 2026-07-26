"""Static API-key operator auth. See docs/adr/0014.

Low-ceremony default for direct API/script/CI use — same posture as
identity_mode="header" for workload identity: a documented dev/simple stub,
not a production access-control system.
"""

from __future__ import annotations

import hmac

from openagent_control.domain.errors import IdentityError


class ApiKeyOperatorAuth:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError(
                "OAC_CONTROL_PLANE_API_KEY must be set to use "
                "operator_auth_mode='api-key' — an empty key would accept every request"
            )
        self._api_key = api_key

    async def identify(self, raw_headers: dict[str, str]) -> str:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise IdentityError("missing 'Authorization: Bearer <api-key>' header")
        if not hmac.compare_digest(token, self._api_key):
            raise IdentityError("invalid control-plane API key")
        return "api-key"
