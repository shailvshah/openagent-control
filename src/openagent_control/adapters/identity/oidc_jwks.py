"""OIDC/JWKS identity adapter for Okta and Microsoft Entra ID. See ADR-0010 and
the `enterprise-idp-integration` skill this was built from.

Validates an access token actually issued by an OIDC-compliant IdP: fetches
the discovery document once at startup, verifies signature/issuer/audience via
a cached, rotation-aware JWKS client, and derives the calling workload's
identity from the client/app-id claim (azp/appid/cid) rather than `sub` —
machine (client-credentials) tokens from both providers identify the calling
application that way; `sub`, when present and distinct, is a delegated human
user and surfaces as `human_sponsor` instead.
"""

from __future__ import annotations

import asyncio

import httpx
import jwt
from jwt import PyJWKClient

from openagent_control.domain.errors import IdentityError
from openagent_control.domain.models import AgentIdentity

_SPONSOR_HEADER = "x-human-sponsor"
_ALGORITHMS = ["RS256"]
# Claims that identify the calling application/service principal, checked in
# order — see ADR-0010 on why this isn't simply `sub`.
_CLIENT_ID_CLAIMS = ("azp", "appid", "cid")


class OidcJwksIdentityProvider:
    def __init__(
        self,
        discovery_url: str,
        audience: str,
        issuer: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        # Fetched once at construction (startup), not lazily: a bad discovery
        # URL/document becomes a startup failure, not a per-request one — same
        # posture as the JWT-SVID trust-bundle key (ADR-0005).
        owns_client = client is None
        discovery_client = client or httpx.Client(timeout=10.0)
        try:
            discovery = discovery_client.get(discovery_url)
            discovery.raise_for_status()
            document = discovery.json()
        finally:
            if owns_client:
                discovery_client.close()

        self._jwks_client = PyJWKClient(document["jwks_uri"])
        self._audience = audience
        self._issuer = issuer or document["issuer"]

    async def identify(self, raw_headers: dict[str, str]) -> AgentIdentity:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise IdentityError("missing 'Authorization: Bearer <access-token>' header")

        try:
            claims = await asyncio.to_thread(self._verify, token)
        except jwt.InvalidTokenError as exc:
            raise IdentityError(f"invalid OIDC access token: {exc}") from exc

        client_id = next((str(claims[c]) for c in _CLIENT_ID_CLAIMS if c in claims), None)
        if not client_id:
            raise IdentityError(
                f"token has none of the expected client-id claims {_CLIENT_ID_CLAIMS}"
            )

        subject = claims.get("sub")
        human_sponsor = str(subject) if subject and subject != client_id else None

        return AgentIdentity(
            spiffe_id=f"oidc://{self._issuer}/{client_id}",
            human_sponsor=human_sponsor or headers.get(_SPONSOR_HEADER),
        )

    def _verify(self, token: str) -> dict[str, object]:
        # Runs in a worker thread (see identify()): PyJWKClient performs a
        # blocking HTTP call on a JWKS cache miss (key rotation), which would
        # otherwise stall the event loop for every concurrent request.
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        claims: dict[str, object] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=_ALGORITHMS,
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["iss", "aud", "exp"]},
        )
        return claims
