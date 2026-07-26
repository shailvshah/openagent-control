"""The agent side of the scenario: a real LangGraph agent whose tools are real
HTTP calls through the governance gateway.

Nothing here is aware of policy, token exchange, or audit. That is the point --
the integration surface for an application team is "call this URL with your
workload's access token", and the control plane does the rest. The tool factory
below is the piece a real team would copy.

Model selection:
  * default -- a deterministic scripted model, so CI and first-run demos are
    reproducible and need no API key;
  * set OAC_SCENARIO_MODEL (e.g. "anthropic:claude-sonnet-4-6") to run the exact
    same graph against a real LLM making its own tool-calling decisions.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

SYSTEM_PROMPT = (
    "You are invoice-bot, a finance agent operating under a governance gateway. "
    "Use read_query to look up invoices. If a tool call is BLOCKED by policy, do "
    "not retry it -- explain the block and stop."
)


def make_gateway_caller(
    gateway_url: str, agent_token: str, subject_token: str | None
) -> Callable[[str, dict[str, Any]], str]:
    """Returns a function that executes one governed tool call.

    `agent_token` is the workload's own OIDC access token (audience: the
    gateway). `subject_token` is the human sponsor's token, forwarded so the
    gateway can perform an RFC 8693 exchange for a downstream-scoped credential
    -- the agent never sees the credential the upstream actually accepts.
    """

    def _call(name: str, arguments: dict[str, Any]) -> str:
        headers = {"Authorization": f"Bearer {agent_token}"}
        if subject_token:
            headers["X-Subject-Token"] = subject_token
        response = httpx.post(
            gateway_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            timeout=15.0,
        )
        body = response.json()
        if "error" in body:
            error = body["error"]
            instruction = error.get("data", {}).get("instruction", "")
            return f"BLOCKED: {error['message']}. {instruction}"
        return json.dumps(body.get("result", body))

    return _call


def build_tools(
    gateway_url: str, agent_token: str, subject_token: str | None
) -> list[Any]:
    """The agent's tools.

    Each declares an explicit typed signature rather than **kwargs: LangChain
    derives the tool's argument schema from the signature, and a **kwargs-only
    function yields an empty schema, so the model's arguments are silently
    dropped before the call is ever made.
    """
    call = make_gateway_caller(gateway_url, agent_token, subject_token)

    @tool
    def read_query(quarter: str) -> str:
        """Read invoice rows for a given quarter, e.g. 'Q3'."""
        return call("read_query", {"quarter": quarter})

    @tool
    def update_record(invoice_id: str, status: str) -> str:
        """Update an invoice's status, e.g. invoice_id 'INV-1001', status 'paid'."""
        return call("update_record", {"invoice_id": invoice_id, "status": status})

    return [read_query, update_record]


class _ScriptedChatModel(GenericFakeChatModel):
    """Deterministic stand-in model; tolerates bind_tools."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _ScriptedChatModel:
        return self


def _scripted_model() -> _ScriptedChatModel:
    return _ScriptedChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_query", "args": {"quarter": "Q3"}, "id": "call_1"}
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "update_record",
                            "args": {"invoice_id": "INV-1001", "status": "written_off"},
                            "id": "call_2",
                        }
                    ],
                ),
                AIMessage(
                    content=(
                        "I retrieved the Q3 invoices. My attempt to write off INV-1001 was "
                        "blocked by policy, so I stopped and am escalating for human approval."
                    )
                ),
            ]
        )
    )


def build_agent(gateway_url: str, agent_token: str, subject_token: str | None) -> Any:
    """The same graph either way -- only the model differs."""
    tools = build_tools(gateway_url, agent_token, subject_token)
    model_id = os.environ.get("OAC_SCENARIO_MODEL")
    model: Any = model_id if model_id else _scripted_model()
    return create_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)


def run_agent(gateway_url: str, agent_token: str, subject_token: str | None, task: str) -> list[Any]:
    agent = build_agent(gateway_url, agent_token, subject_token)
    result = agent.invoke({"messages": [{"role": "user", "content": task}]})
    messages: list[Any] = result["messages"]
    return messages
