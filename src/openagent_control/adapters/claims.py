"""Claim helpers shared by every adapter that reads an OIDC token.

Extracted so the operator-auth adapter (ADR-0014) and the subject verifier
(ADR-0019) resolve role claims identically. They face the same problem —
roles are not a standard OIDC claim, and each provider nests them differently
— and two copies of that logic would drift on exactly the per-provider quirks
it exists to absorb.
"""

from __future__ import annotations


def resolve_dotted_claim(claims: dict[str, object], dotted_path: str) -> object:
    """Walks a dotted claim path (e.g. "realm_access.roles") through nested
    dicts. Returns None if any segment is missing or not a dict along the way.

    Needed because providers disagree about shape: Okta wants a custom
    top-level `groups` claim, Entra uses top-level `roles`, and Keycloak nests
    realm roles under `realm_access.roles`.
    """
    value: object = claims
    for segment in dotted_path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value
