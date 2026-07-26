"""OIDC/JWKS operator auth for the control plane. See docs/adr/0014.

Reuses the discovery/JWKS-fetch pattern from adapters/identity/oidc_jwks.py's
OidcJwksIdentityProvider (fetch the discovery document once at construction,
verify signature/issuer/audience via a cached, rotation-aware JWKS client),
but answers a different question: not "what workload is this" but "is this
human allowed to operate the control plane" — checked via a configurable
role/group claim rather than derived into a SPIFFE-shaped identity.

`role_claim` supports a dotted path for providers that nest roles inside an
object rather than exposing a top-level array, so one adapter covers all
three IdPs this project already targets elsewhere:

- **Okta**: groups are not a default access-token claim — you must add a
  custom claim (e.g. named "groups") to the authorization server's claims
  mapping. Set OAC_CONTROL_PLANE_OIDC_ROLE_CLAIM=groups.
- **Microsoft Entra ID**: prefer the `roles` claim (app roles assigned to the
  user, a flat array of strings) over `groups`. Entra's `groups` claim is
  subject to "claim overage" — if a user belongs to more groups than fit in
  the token, Entra replaces it with a `_claim_names`/`_claim_sources`
  indirection pointing at the Graph API instead of inline group names, which
  this adapter does not follow. App roles don't have this problem; set
  OAC_CONTROL_PLANE_OIDC_ROLE_CLAIM=roles.
- **Keycloak**: realm roles live under `realm_access.roles` (a nested object),
  not a top-level claim. Set
  OAC_CONTROL_PLANE_OIDC_ROLE_CLAIM=realm_access.roles — the dotted path is
  resolved by walking nested dicts.
"""

from __future__ import annotations

import asyncio

import httpx
import jwt
from jwt import PyJWKClient

from openagent_control.domain.errors import IdentityError

_ALGORITHMS = ["RS256"]


def _resolve_dotted_claim(claims: dict[str, object], dotted_path: str) -> object:
    """Walks a dotted claim path (e.g. "realm_access.roles") through nested
    dicts. Returns None if any segment is missing or not a dict along the way."""
    value: object = claims
    for segment in dotted_path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


class OidcOperatorAuth:
    def __init__(
        self,
        discovery_url: str,
        audience: str,
        role_claim: str,
        required_role: str,
        issuer: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        # Fetched once at construction (startup): an unreachable discovery
        # endpoint is a startup failure, same posture as OidcJwksIdentityProvider
        # (ADR-0010) and VaultTransitSigner (ADR-0013).
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
        self._role_claim = role_claim
        self._required_role = required_role

    async def identify(self, raw_headers: dict[str, str]) -> str:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise IdentityError("missing 'Authorization: Bearer <access-token>' header")

        try:
            claims = await asyncio.to_thread(self._verify, token)
        except jwt.InvalidTokenError as exc:
            raise IdentityError(f"invalid OIDC access token: {exc}") from exc

        roles = _resolve_dotted_claim(claims, self._role_claim)
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(roles, list) or self._required_role not in roles:
            raise IdentityError(
                f"token lacks required role '{self._required_role}' in claim "
                f"'{self._role_claim}'"
            )

        subject = claims.get("preferred_username") or claims.get("sub")
        return str(subject)

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
