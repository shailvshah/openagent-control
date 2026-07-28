"""Day-1 demo: a Google ADK agent whose every tool call is governed by
openagent-control.

The agent: (1) reads invoice data -- ALLOWED by OPA policy and forwarded
upstream; (2) tries to update a Salesforce account -- DENIED (capability not
granted to this agent identity in registry/agents.yaml); (3) reads the
gateway's semantic denial and stops, instead of retrying.

Run the stack first (gateway :8000, OPA :8181):  make up-demo
Then, in a SEPARATE venv (see README -- google-adk's own dependencies conflict
with this repo's pinned versions):
    poetry run python -m examples.google_adk_governed_agent.demo

Model selection:
  * default -- calls the governed tools directly, no LLM involved, so this
    runs offline with zero API keys and deterministic output;
  * set ANTHROPIC_API_KEY (and optionally ADK_MODEL) to have a real model
    decide the tool calls instead, via ADK's LiteLlm wrapper (ADK's native
    models are Gemini; LiteLlm is ADK's own documented path to any other
    provider). Verified end-to-end with "anthropic/claude-haiku-4-5-20251001"
    (the default).
"""

from __future__ import annotations

import asyncio
import os

from examples.google_adk_governed_agent.governed_tools import DEFAULT_GATEWAY_URL, build_tools
from google.adk.runners import InMemoryRunner
from google.genai import types

APP_NAME = "invoice_bot"
USER_ID = "demo-user"
SESSION_ID = "demo-session"


def _run_direct(tools: list) -> None:
    """Zero-API-key path: call the governed tools directly, proving the real
    ALLOW/DENY decisions and receipts without needing a live model to decide
    to call them."""
    read_query, salesforce_update_account = tools
    print("=" * 72)
    print("[direct] read_query(quarter='Q3') ->")
    print(" ", read_query(quarter="Q3"))
    print("[direct] salesforce_update_account(account='ACME', credit_limit=50000) ->")
    print(" ", salesforce_update_account(account="ACME", credit_limit=50000))
    print("=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


async def _run_with_model_async(tools: list, model_id: str) -> None:
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    agent = Agent(
        name=APP_NAME,
        model=LiteLlm(model=model_id),
        instruction="You are invoice-bot, a governed finance agent.",
        tools=tools,
    )
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text="Summarize Q3 invoices and raise ACME's credit limit to 50k.")],
    )
    print("=" * 72)
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    print(f"[tool_call] {part.function_call.name}({dict(part.function_call.args)})")
                if getattr(part, "text", None):
                    print(f"[{event.author}] {part.text}")
    print("=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


def main() -> None:
    gateway_url = os.environ.get("OAC_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    tools = build_tools(gateway_url)

    if os.environ.get("ANTHROPIC_API_KEY"):
        model_id = os.environ.get("ADK_MODEL", "anthropic/claude-haiku-4-5-20251001")
        asyncio.run(_run_with_model_async(tools, model_id))
    else:
        _run_direct(tools)


if __name__ == "__main__":
    main()
