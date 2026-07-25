"""MCP gateway route: the interception point described in docs/design.md section 2.

Flow: identify -> evaluate policy -> (deny: semantic error payload) | (allow: exchange
token if delegated, forward to upstream) -> audit.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from openagent_control.domain.models import Decision, ToolCallRequest
from openagent_control.gateway.dependencies import Container, get_container

router = APIRouter()

_DELEGATED_AUDIENCE = "openagent-control-mcp-upstream"


@router.post("/mcp/v1")
async def mcp_proxy(
    request: Request, container: Annotated[Container, Depends(get_container)]
) -> dict[str, Any]:
    body = await request.json()
    agent = await container.identity_provider.identify(dict(request.headers))

    call = ToolCallRequest(
        method=body.get("method", ""),
        tool_name=(body.get("params") or {}).get("name"),
        arguments=(body.get("params") or {}).get("arguments", {}),
        agent=agent,
        request_id=body.get("id"),
    )

    decision = await container.policy_engine.evaluate(call)
    receipt = await container.ledger.record(agent, call, decision)
    await container.audit_exporter.export(receipt)

    if decision.decision is not Decision.ALLOW:
        return {
            "jsonrpc": "2.0",
            "id": call.request_id,
            "error": {
                "code": -32000,
                "message": f"Policy violation: {decision.reason}",
                "data": {"instruction": "Stop execution and request user approval."},
            },
        }

    if agent.human_sponsor:
        subject_token = request.headers.get("x-subject-token", "")
        credential = await container.token_exchange.exchange(subject_token, _DELEGATED_AUDIENCE)
    else:
        credential = f"autonomous::{agent.spiffe_id}"

    return await container.mcp_upstream.forward(call, credential)
