"""JWT-SVID identity adapter: cryptographic workload identity (ADR-0005).

Validates a SPIFFE JWT-SVID presented as `Authorization: Bearer <token>`:
signature against the trust-domain public key, audience, and expiry; the SPIFFE
ID is the token's `sub` claim. SPIRE issues exactly this token shape via its
Workload API (`spire-agent api fetch jwt`), so pointing `public_key_path` at the
trust bundle key makes this the production identity path — unlike the header
adapter, callers cannot claim an identity they cannot prove.

The per-request human sponsor stays a header (`X-Human-Sponsor`): it is an
assertion about delegation context, verified separately via the subject-token
exchange (ADR-0004), not part of workload identity.
"""

from __future__ import annotations

from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.ed448 import Ed448PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from openagent_control.domain.errors import IdentityError
from openagent_control.domain.models import AgentIdentity

_SPONSOR_HEADER = "x-human-sponsor"
_ALGORITHMS = ["RS256", "ES256", "EdDSA"]
_SUPPORTED_KEY_TYPES = (RSAPublicKey, EllipticCurvePublicKey, Ed25519PublicKey, Ed448PublicKey)


class JwtSvidIdentityProvider:
    def __init__(self, public_key_path: str | Path, audience: str) -> None:
        # Parse the PEM once at startup: handing PyJWT the raw PEM string makes
        # it re-parse the key on every decode (~56µs/request measured for
        # RSA-2048); a preloaded key object cuts verification ~3x. Rejecting
        # unsupported key types here turns a bad trust-bundle config into a
        # startup failure instead of a per-request one.
        key = load_pem_public_key(Path(public_key_path).read_bytes())
        if not isinstance(key, _SUPPORTED_KEY_TYPES):
            raise ValueError(
                f"unsupported trust-bundle key type {type(key).__name__}; "
                f"expected one usable with {_ALGORITHMS}"
            )
        self._public_key: (
            RSAPublicKey | EllipticCurvePublicKey | Ed25519PublicKey | Ed448PublicKey
        ) = key
        self._audience = audience

    async def identify(self, raw_headers: dict[str, str]) -> AgentIdentity:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise IdentityError("missing 'Authorization: Bearer <jwt-svid>' header")

        try:
            claims = jwt.decode(
                token,
                key=self._public_key,
                algorithms=_ALGORITHMS,
                audience=self._audience,
                options={"require": ["sub", "exp", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            raise IdentityError(f"invalid JWT-SVID: {exc}") from exc

        spiffe_id = str(claims["sub"])
        if not spiffe_id.startswith("spiffe://"):
            raise IdentityError(f"token subject is not a SPIFFE ID: {spiffe_id!r}")

        return AgentIdentity(spiffe_id=spiffe_id, human_sponsor=headers.get(_SPONSOR_HEADER))
