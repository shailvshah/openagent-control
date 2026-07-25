"""Stub RFC 8693 token exchange. See docs/adr/0004-mcp-as-the-v1-protocol-surface.md.

Real adapters (Okta, Microsoft Entra ID) implement the same `TokenExchange` port —
see docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.
"""

from __future__ import annotations


class StubTokenExchange:
    async def exchange(self, subject_token: str, audience: str) -> str:
        return f"stub-obo-token::aud={audience}::subj={subject_token[:8]}"
