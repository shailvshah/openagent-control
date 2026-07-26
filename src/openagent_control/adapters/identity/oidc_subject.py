"""Verifies the human subject token on a delegated call. See ADR-0019.

The gateway previously relayed `X-Subject-Token` to the IdP's token-exchange
endpoint without ever looking inside it. The IdP would reject a forged token,
so that much was safe — but the gateway could not tell *who* the call actually
ran as, could not check that the user matched the sponsor the agent claimed,
and could not let policy reason about the user's own entitlements. Approval and
authorization were the same field.

This adapter closes that: it validates the subject token the same way the
workload's token is validated (JWKS, signature, `iss`, `aud`, `exp`) and
projects it into a `SubjectIdentity` — the authorization principal, its roles,
and its scopes.

**Spec notes that shaped this, rather than being assumed:**

- `subject_id` is `{issuer}#{sub}`, never bare `sub`. OIDC Core §5.7: the only
  guaranteed unique identifier for an end-user is the `iss`/`sub` pair, because
  `sub` is only locally unique within an issuer.
- `preferred_username`/`email` are captured for display only. The same section
  says they MUST NOT be used as identifiers — they are mutable and reassignable.
- An **ID token is not an access token** and must never authorize an API call.
  No special-case check is needed: validating `aud` against this resource's
  audience rejects one naturally, since an ID token's `aud` is the client id.
  There is a test asserting exactly that, so the property is not accidental.
- `may_act` (RFC 8693 §4.4) is the standard way a subject token names the party
  authorized to act for that user. Where the IdP issues it, it is a stronger
  binding than comparing subject identifiers — see `_authorized_actor`.
- Roles are not a standard OIDC claim, so the claim name is configurable, with
  the same per-provider guidance as `OidcOperatorAuth`: Okta needs a custom
  `groups` claim, Entra prefers `roles` (its `groups` claim suffers overage),
  Keycloak nests them at `realm_access.roles`.
"""

from __future__ import annotations

import asyncio

import httpx
import jwt
from jwt import PyJWKClient

from openagent_control.adapters.claims import resolve_dotted_claim
from openagent_control.domain.errors import IdentityError
from openagent_control.domain.models import SubjectIdentity

_ALGORITHMS = ["RS256"]


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _authorized_actor(claims: dict[str, object]) -> str | None:
    """Pulls `may_act.sub` — the party this user authorized to act for them.

    RFC 8693 models `may_act` as an object identifying a party, so the `sub`
    inside it is the actor's own subject (for a workload, its client id), not
    the user's. Absent on most IdPs today, which is why binding cannot depend
    on it alone.
    """
    may_act = claims.get("may_act")
    if not isinstance(may_act, dict):
        return None
    actor = may_act.get("sub") or may_act.get("client_id")
    return str(actor) if actor else None


class OidcSubjectVerifier:
    def __init__(
        self,
        discovery_url: str,
        audience: str,
        role_claim: str = "roles",
        issuer: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        # Fetched once at construction (startup), so a bad discovery document
        # is a startup failure rather than a per-request one — the same posture
        # as OidcJwksIdentityProvider (ADR-0010) and VaultTransitSigner.
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

    async def verify(self, subject_token: str) -> SubjectIdentity:
        if not subject_token:
            raise IdentityError("delegated call is missing a subject token")
        try:
            claims = await asyncio.to_thread(self._verify, subject_token)
        except jwt.InvalidTokenError as exc:
            raise IdentityError(f"invalid subject token: {exc}") from exc

        subject = claims.get("sub")
        if not subject:
            raise IdentityError("subject token has no 'sub' claim")

        roles = resolve_dotted_claim(claims, self._role_claim)
        username = claims.get("preferred_username") or claims.get("email")

        return SubjectIdentity(
            subject_id=f"{self._issuer}#{subject}",
            issuer=self._issuer,
            username=str(username) if username else None,
            roles=_string_list(roles),
            scopes=_string_list(claims.get("scope")),
            authorized_actor=_authorized_actor(claims),
        )

    def _verify(self, token: str) -> dict[str, object]:
        # Runs in a worker thread: PyJWKClient makes a blocking HTTP call on a
        # JWKS cache miss (key rotation), which would otherwise stall the event
        # loop for every concurrent request.
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
