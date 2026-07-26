"""Authorize-only route: decide and receipt a tool call without running it.

The gateway's other routes are proxies — the agent's call goes through them to
an upstream MCP server. That only suits an agent whose tools already live
behind MCP. An agent already running in production usually has its tool
functions in its own process, calling Salesforce or a database directly, and
moving that code behind a proxy to gain governance is a rewrite nobody
schedules.

This is the other half of ADR-0001's hybrid: the agent keeps its code and asks
"may I?" immediately before running it. Same identity check, same policy
engine, same signed receipt, no proxying — see `openagent_control.sdk`, which
is the client for this endpoint, and ADR-0017.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from openagent_control.domain.errors import IdentityError, MissingSubjectTokenError
from openagent_control.gateway.dependencies import Container, get_container

router = APIRouter()


class AuthorizeRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AuthorizeResponse(BaseModel):
    """Deliberately not a JSON-RPC envelope: this is not a tool call being
    forwarded, and dressing a decision up as one would imply the tool ran."""

    allowed: bool
    decision: str
    reason: str = ""
    shadowed: bool = False
    """True when the policy said DENY but decision_mode="observe" let the call
    proceed anyway (ADR-0012). The SDK surfaces it so an operator running a
    shadow rollout can tell "allowed" from "would have been blocked"."""
    instruction: str = ""
    receipt_id: str
    receipt_signature: str | None = None


@router.post("/api/v1/authorize")
async def authorize(
    body: AuthorizeRequest,
    request: Request,
    container: Annotated[Container, Depends(get_container)],
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": None,
        "method": "tools/call",
        "params": {"name": body.tool, "arguments": body.arguments},
    }
    try:
        outcome = await container.governed_execution.authorize(dict(request.headers), payload)
    except IdentityError as exc:
        return JSONResponse(status_code=401, content={"detail": f"Identity error: {exc}"})
    except MissingSubjectTokenError as exc:
        return JSONResponse(status_code=401, content={"detail": f"Delegation error: {exc}"})

    return AuthorizeResponse(
        allowed=outcome.allowed,
        decision=outcome.decision.decision.value,
        reason=outcome.decision.reason,
        shadowed=outcome.shadowed,
        instruction="" if outcome.allowed else "Stop execution and request user approval.",
        receipt_id=outcome.receipt.sequence_id,
        receipt_signature=outcome.receipt.signature,
    )
