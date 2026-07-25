from __future__ import annotations

import pytest

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.domain.errors import IdentityError


@pytest.mark.asyncio
async def test_identify_reads_spiffe_and_sponsor_headers() -> None:
    provider = HeaderIdentityProvider()

    agent = await provider.identify(
        {
            "X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "X-Human-Sponsor": "alice@corp.net",
        }
    )

    assert agent.spiffe_id == "spiffe://corp.net/ns/finance/agent/invoice-bot"
    assert agent.human_sponsor == "alice@corp.net"


@pytest.mark.asyncio
async def test_identify_raises_identity_error_without_spiffe_header() -> None:
    provider = HeaderIdentityProvider()

    with pytest.raises(IdentityError, match="x-spiffe-id"):
        await provider.identify({})
