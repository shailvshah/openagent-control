"""MCP gateway route: thin HTTP/JSON-RPC adapter over GovernedExecutionService.

All governance logic lives in the application layer; this route only parses the
transport envelope and maps domain errors to HTTP status codes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from openagent_control.domain.errors import IdentityError, MissingSubjectTokenError
from openagent_control.gateway.dependencies import Container, get_container

router = APIRouter()


def _jsonrpc_error_response(
    status_code: int, request_id: Any, code: int, message: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
    )


@router.post("/mcp/v1")
async def mcp_proxy(
    request: Request, container: Annotated[Container, Depends(get_container)]
) -> Any:
    try:
        body = await request.json()
    except ValueError:
        return _jsonrpc_error_response(400, None, -32700, "Parse error: body is not valid JSON")
    if not isinstance(body, dict):
        return _jsonrpc_error_response(
            400, None, -32600, "Invalid request: body must be a JSON-RPC object"
        )

    try:
        return await container.governed_execution.execute(dict(request.headers), body)
    except IdentityError as exc:
        return _jsonrpc_error_response(401, body.get("id"), -32001, f"Identity error: {exc}")
    except MissingSubjectTokenError as exc:
        return _jsonrpc_error_response(401, body.get("id"), -32003, f"Delegation error: {exc}")
