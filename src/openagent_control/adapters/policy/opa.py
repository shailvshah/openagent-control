"""OPA/Rego policy engine adapter. See docs/adr/0002-opa-rego-as-the-v1-policy-engine.md."""

from __future__ import annotations

import httpx

from openagent_control.domain.errors import PolicyEngineUnavailableError
from openagent_control.domain.models import Decision, PolicyDecision, ToolCallRequest


class OPAPolicyEngine:
    """Evaluates a tool call against OPA's HTTP API."""

    def __init__(self, opa_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._opa_url = opa_url
        self._client = client or httpx.AsyncClient(timeout=5.0)

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        opa_input = {
            "input": {
                "method": request.method,
                "spiffe_id": request.agent.spiffe_id,
                # Registry facts (ADR-0008): policy logic evaluates against these
                # instead of data hardcoded in the Rego file.
                "agent": (
                    request.registration.model_dump(mode="json") if request.registration else None
                ),
                "params": {
                    "name": request.tool_name,
                    "arguments": request.arguments,
                },
            }
        }
        try:
            response = await self._client.post(self._opa_url, json=opa_input)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PolicyEngineUnavailableError(str(exc)) from exc
        result = response.json().get("result", {})

        decision = Decision.ALLOW if result.get("allow", False) else Decision.DENY
        reason = result.get("reason", "" if decision is Decision.ALLOW else "Denied by policy")
        return PolicyDecision(decision=decision, reason=reason)

    async def aclose(self) -> None:
        await self._client.aclose()
