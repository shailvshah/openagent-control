"""LangChain / LangGraph integration. See ADR-0017.

`langchain` is imported inside the functions, never at module scope: it is not
a dependency of this project, and importing `openagent_control.sdk` must not
require it. Install it yourself (`pip install langchain`) — pinning a version
here would put this project in the middle of an agent framework's release
cadence for no benefit.

Two shapes, matching the SDK's two integration patterns:

- `govern(fn, client)` — your function keeps doing the work; the gateway
  decides whether it runs. Use this for an agent already in production whose
  tools call Salesforce or a database directly.
- `proxied_tools(client)` — tools that live on an MCP server behind the
  gateway, turned into LangChain tools automatically from whatever this agent
  is granted. Use this when the tools are not your code.

Both return ordinary LangChain `BaseTool`s, so they drop into
`create_agent(...)`, a LangGraph `ToolNode`, or anything else that takes a tool
list, with nothing else changed.

Denials come back as tool *output*, not exceptions (`on_deny="return"`), so the
model reads "BLOCKED: ... Stop execution and request user approval." and halts.
An exception here would propagate out of the tool node and end the run, turning
a working policy decision into a crash.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openagent_control.sdk.client import AsyncGovernedClient, GovernedClient, ToolCallFailed
from openagent_control.sdk.decorator import governed


def _tool_factory() -> Any:
    try:
        from langchain.tools import tool
    except ImportError as exc:  # pragma: no cover - exercised only without langchain
        raise ImportError(
            "openagent_control.sdk.langchain needs LangChain installed: pip install langchain"
        ) from exc
    return tool


def _structured_tool() -> Any:
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:  # pragma: no cover - exercised only without langchain
        raise ImportError(
            "openagent_control.sdk.langchain needs LangChain installed: pip install langchain"
        ) from exc
    return StructuredTool


_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _args_model(tool_name: str, input_schema: dict[str, Any]) -> Any:
    """Turns an MCP tool's JSON Schema into a pydantic model for LangChain.

    Without this the proxy is a bare `**kwargs` function, and LangChain — which
    builds a tool's argument schema by introspecting the callable — has nothing
    to introspect. It then passes no arguments at all, and the upstream rejects
    the call for a missing required field. The tool looks broken; the cause is
    a schema that was never handed over. MCP advertises `inputSchema` precisely
    so this is not guesswork.
    """
    from pydantic import Field, create_model

    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    required = set(input_schema.get("required") or [])

    fields: dict[str, Any] = {}
    for field_name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        annotation = _JSON_TYPES.get(str(spec.get("type", "")), Any)
        description = spec.get("description") or ""
        if field_name in required:
            fields[field_name] = (annotation, Field(description=description))
        else:
            fields[field_name] = (annotation | None, Field(default=None, description=description))
    return create_model(f"{tool_name}Args", **fields)


def govern(
    func: Callable[..., Any],
    client: GovernedClient | AsyncGovernedClient,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """Wraps one of your own functions as a governed LangChain tool.

    The name, argument schema, and description LangChain infers all come from
    `func` itself — `@governed` preserves them via functools.wraps, so the model
    sees exactly the tool it would have seen ungoverned.
    """
    tool = _tool_factory()
    wrapped = governed(client, name=name, on_deny="return")(func)
    if description:
        return tool(name or func.__name__, description=description)(wrapped)
    return tool(wrapped)


def proxied_tools(client: GovernedClient) -> list[Any]:
    """Every tool this agent is granted, as LangChain tools that execute
    through the gateway.

    The listing is the gateway's, already filtered to the agent's registry
    grants (ADR-0016) — so the model is never shown a tool that a call would
    then be denied for, which is the failure that makes governance look broken
    from inside an agent.
    """
    structured_tool = _structured_tool()
    tools = []
    for spec in client.list_tools():
        tool_name = spec.get("name")
        if not isinstance(tool_name, str):
            continue
        input_schema = spec.get("inputSchema")
        tools.append(
            structured_tool.from_function(
                func=_proxy_callable(client, tool_name),
                name=tool_name,
                description=spec.get("description") or tool_name,
                args_schema=_args_model(
                    tool_name, input_schema if isinstance(input_schema, dict) else {}
                ),
            )
        )
    return tools


def _proxy_callable(client: GovernedClient, tool_name: str) -> Callable[..., str]:
    def call(**arguments: Any) -> str:
        # Drop the nulls LangChain fills in for absent optional fields: an
        # upstream that distinguishes "absent" from "null" would otherwise see
        # an explicit null it was never sent.
        supplied = {k: v for k, v in arguments.items() if v is not None}
        try:
            result = client.call_tool(tool_name, supplied)
        except ToolCallFailed as exc:
            # Already carries the gateway's stop instruction; handing it back as
            # output is what makes the model halt instead of retry-looping.
            return str(exc)
        return str(result)

    call.__name__ = tool_name
    return call
