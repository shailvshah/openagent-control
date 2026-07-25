"""Header-trusting identity adapter. See docs/adr/0005.

NOT a security boundary on its own: it trusts whatever `X-Spiffe-ID` header is
present. Only safe behind a network layer (e.g. a service mesh) that has already
performed real SPIFFE/mTLS attestation. Swap for a SPIRE Workload API adapter before
this is exposed to anything untrusted.
"""

from __future__ import annotations

from openagent_control.domain.models import AgentIdentity

_SPIFFE_HEADER = "x-spiffe-id"
_SPONSOR_HEADER = "x-human-sponsor"


class HeaderIdentityProvider:
    async def identify(self, raw_headers: dict[str, str]) -> AgentIdentity:
        headers = {k.lower(): v for k, v in raw_headers.items()}
        spiffe_id = headers.get(_SPIFFE_HEADER)
        if not spiffe_id:
            raise ValueError(f"missing required '{_SPIFFE_HEADER}' header")
        return AgentIdentity(spiffe_id=spiffe_id, human_sponsor=headers.get(_SPONSOR_HEADER))
