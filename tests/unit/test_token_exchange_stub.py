from __future__ import annotations

import pytest

from openagent_control.adapters.token_exchange.stub import StubTokenExchange


@pytest.mark.asyncio
async def test_exchange_returns_audience_scoped_token() -> None:
    result = await StubTokenExchange().exchange("subject-token-123", "target-audience")

    assert "target-audience" in result
    assert "subject-" in result
