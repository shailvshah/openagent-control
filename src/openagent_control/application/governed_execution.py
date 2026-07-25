"""The governed-execution use case: the transport-agnostic core of the gateway.

Every deployment pattern from ADR-0001 (egress gateway today; sidecar and native SDK
later) drives this same service rather than re-implementing the sequence:

    identify -> evaluate policy -> record + export receipt
             -> (deny: semantic error) | (allow: broker credential, forward upstream)

Failure posture:
- Policy engine unreachable  => fail closed: an explicit DENY is recorded and
  exported, so the outage itself leaves an audit trail (never a silent 500).
- Upstream failure after ALLOW => JSON-RPC error with a stop instruction, so an LLM
  agent halts instead of retry-looping; the ALLOW receipt has already been recorded.
- Identity / missing subject token => typed errors for the transport layer to map
  (HTTP 401 in the FastAPI adapter).
"""

from __future__ import annotations

from typing import Any

from openagent_control.domain.errors import (
    MissingSubjectTokenError,
    PolicyEngineUnavailableError,
    UpstreamError,
)
from openagent_control.domain.models import (
    AgentIdentity,
    Decision,
    PolicyDecision,
    ToolCallRequest,
)
from openagent_control.domain.ports import (
    AuditExporter,
    IdentityProvider,
    Ledger,
    MCPUpstream,
    PolicyEngine,
    TokenExchange,
)

_SUBJECT_TOKEN_HEADER = "x-subject-token"

_STOP_INSTRUCTION = "Stop execution and request user approval."
_FAIL_CLOSED_REASON = "Policy engine unavailable; denied (fail-closed)"


class GovernedExecutionService:
    def __init__(
        self,
        *,
        identity_provider: IdentityProvider,
        policy_engine: PolicyEngine,
        ledger: Ledger,
        audit_exporter: AuditExporter,
        token_exchange: TokenExchange,
        mcp_upstream: MCPUpstream,
        delegated_audience: str,
    ) -> None:
        self._identity_provider = identity_provider
        self._policy_engine = policy_engine
        self._ledger = ledger
        self._audit_exporter = audit_exporter
        self._token_exchange = token_exchange
        self._mcp_upstream = mcp_upstream
        self._delegated_audience = delegated_audience

    async def execute(self, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        """Runs one governed tool call; returns a JSON-RPC response object.

        Raises IdentityError or MissingSubjectTokenError for the transport layer
        to map to its own auth failure shape.
        """
        agent = await self._identity_provider.identify(headers)

        params = payload.get("params") or {}
        call = ToolCallRequest(
            method=payload.get("method", ""),
            tool_name=params.get("name"),
            arguments=params.get("arguments", {}),
            agent=agent,
            request_id=payload.get("id"),
        )

        try:
            decision = await self._policy_engine.evaluate(call)
        except PolicyEngineUnavailableError:
            decision = PolicyDecision(decision=Decision.DENY, reason=_FAIL_CLOSED_REASON)

        receipt = await self._ledger.record(agent, call, decision)
        await self._audit_exporter.export(receipt)

        if decision.decision is not Decision.ALLOW:
            return _jsonrpc_error(
                call.request_id,
                code=-32000,
                message=f"Policy violation: {decision.reason}",
                instruction=_STOP_INSTRUCTION,
            )

        credential = await self._broker_credential(agent, headers)

        try:
            return await self._mcp_upstream.forward(call, credential)
        except UpstreamError as exc:
            return _jsonrpc_error(
                call.request_id,
                code=-32002,
                message=f"Upstream execution failed: {exc}",
                instruction="Do not retry automatically; report the failure to the user.",
            )

    async def _broker_credential(self, agent: AgentIdentity, headers: dict[str, str]) -> str:
        if not agent.human_sponsor:
            return f"autonomous::{agent.spiffe_id}"
        subject_token = {k.lower(): v for k, v in headers.items()}.get(_SUBJECT_TOKEN_HEADER)
        if not subject_token:
            raise MissingSubjectTokenError(
                f"delegated call for sponsor '{agent.human_sponsor}' "
                f"requires a '{_SUBJECT_TOKEN_HEADER}' header"
            )
        return await self._token_exchange.exchange(subject_token, self._delegated_audience)


def _jsonrpc_error(
    request_id: str | int | None, *, code: int, message: str, instruction: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"instruction": instruction}},
    }
