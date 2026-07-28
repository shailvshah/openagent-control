"""Day-1 demo: a LangGraph agent whose every tool call is governed by openagent-control.

The scripted invoice-bot: (1) reads invoice data -- ALLOWED by OPA policy and
forwarded upstream; (2) tries to update a Salesforce account -- DENIED (capability
not granted to this agent identity); (3) reads the gateway's semantic denial and
stops gracefully, asking for human approval. Every decision -- including the denial
-- was recorded as an Ed25519 hash-chained receipt in the gateway's audit log.

Run the stack first (gateway :8000, OPA :8181, mock upstream):  make up
Then:  poetry run python -m examples.langgraph_governed_agent.demo

Model selection:
  * default -- a deterministic scripted model (ScriptedChatModel), so this runs
    offline with zero API keys and identical output every time;
  * set OAC_DEMO_MODEL (e.g. "anthropic:claude-sonnet-4-6") to run the exact
    same governed tools against a real LLM making its own tool-calling
    decisions. Needs `poetry install --with examples` (installs
    langchain-anthropic) and a real `ANTHROPIC_API_KEY` in the environment --
    read directly by langchain-anthropic, not by this project, so there is
    nothing here to configure beyond the two environment variables.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.agents import create_agent

from examples.langgraph_governed_agent.governed_tools import (
    DEFAULT_GATEWAY_URL,
    DEFAULT_TOKEN_URL,
    demo_tools,
    fetch_agent_token,
)
from examples.langgraph_governed_agent.scripted_model import scripted_finance_model


def main() -> None:
    gateway_url = os.environ.get("OAC_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    token_url = os.environ.get("OAC_TOKEN_URL", DEFAULT_TOKEN_URL)

    # `make up` runs the gateway with real OIDC identity, so the agent fetches
    # its own access token first, exactly as a service principal would. Set
    # OAC_IDENTITY_MODE=header on the gateway to use the dev header stub instead.
    agent_token = None if os.environ.get("OAC_USE_HEADER_IDENTITY") else fetch_agent_token(token_url)

    # A model id string here (e.g. "anthropic:claude-sonnet-4-6") is resolved by
    # LangChain's own init_chat_model, which reads ANTHROPIC_API_KEY from the
    # environment -- the same "no API key needed unless you opt in" posture
    # examples/enterprise_scenario/agent.py already uses for OAC_SCENARIO_MODEL.
    model_id = os.environ.get("OAC_DEMO_MODEL")
    model: Any = model_id if model_id else scripted_finance_model()

    agent = create_agent(
        model=model,
        tools=demo_tools(gateway_url, agent_token),
        system_prompt="You are invoice-bot, a governed finance agent.",
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Summarize Q3 invoices and raise ACME's credit limit to 50k.",
                }
            ]
        }
    )

    print("=" * 72)
    for message in result["messages"]:
        role = message.__class__.__name__
        content = message.content or ""
        for tool_call in getattr(message, "tool_calls", []) or []:
            print(f"[{role}] -> tool_call {tool_call['name']}({tool_call['args']})")
        if content:
            print(f"[{role}] {content}")
    print("=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


if __name__ == "__main__":
    main()
