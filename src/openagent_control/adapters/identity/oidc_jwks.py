"""OIDC/JWKS identity adapter for Okta and Microsoft Entra ID. See ADR-0010 and
the `enterprise-idp-integration` skill this was built from.

Validates an access token actually issued by an OIDC-compliant IdP: fetches
the discovery document once at startup, verifies signature/issuer/audience via
a cached, rotation-aware JWKS client, and derives the calling workload's
identity from the client/app-id claim (azp/appid/cid) rather than `sub` —
machine (client-credentials) tokens identify the calling application that way.

A `sub` claim is treated as a delegated human sponsor only when the token is
not an app-only token; see `_has_human_sponsor`, which encodes each provider's
documented marker. Verified against a real Keycloak realm — see
tests/integration/test_keycloak_conformance.py.
"""

from __future__ import annotations

import asyncio

import httpx
import jwt
from jwt import PyJWKClient

from openagent_control.domain.errors import IdentityError
from openagent_control.domain.models import AgentIdentity

_ALGORITHMS = ["RS256"]
# Claims that identify the calling application/service principal, checked in
# order — see ADR-0010 on why this isn't simply `sub`.
_CLIENT_ID_CLAIMS = ("azp", "appid", "cid")

# Keycloak issues client-credentials tokens with `sub` set to the service
# account's UUID — distinct from the client id — so "sub differs from the
# client" is not on its own evidence of a human. Each provider marks app-only
# tokens differently; these are the documented markers:
_KEYCLOAK_SERVICE_ACCOUNT_PREFIX = "service-account-"
_ENTRA_APP_ONLY_IDTYP = "app"


def _has_human_sponsor(claims: dict[str, object], client_id: str) -> bool:
    """True only when the token really represents a user acting through the agent.

    Getting this wrong is not cosmetic: a false positive makes the gateway treat
    an autonomous machine call as delegated and reject it for a missing subject
    token, which is exactly what happened against a real Keycloak realm before
    this check existed.
    """
    subject = claims.get("sub")
    if not subject or subject == client_id:  # Okta client-credentials
        return False
    if claims.get("idtyp") == _ENTRA_APP_ONLY_IDTYP:  # Entra app-only token
        return False
    username = claims.get("preferred_username")  # Keycloak service account
    return not (isinstance(username, str) and username.startswith(_KEYCLOAK_SERVICE_ACCOUNT_PREFIX))


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

        # Issuer-scoped, like the spiffe_id below: OIDC Core §5.7 says `sub`
        # is only locally unique within an issuer, so a bare `sub` would hold
        # human identity to a weaker standard than workload identity and would
        # collide across federated issuers (ADR-0019).
        human_sponsor = (
            f"{self._issuer}#{claims['sub']}" if _has_human_sponsor(claims, client_id) else None
        )

        # No X-Human-Sponsor fallback here, deliberately. Trusting a header to
        # name the human would let an autonomous agent assert it acts for
        # anyone, which is a dev-stub property (ADR-0005) that has no business
        # in the production identity path. The header remains honoured by
        # HeaderIdentityProvider, where the whole identity is already a stub.
        return AgentIdentity(
            spiffe_id=f"oidc://{self._issuer}/{client_id}",
            human_sponsor=human_sponsor,
            client_id=client_id,
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
