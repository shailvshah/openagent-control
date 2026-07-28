"""Day-1 demo: a Strands Agent whose every tool call is governed by
openagent-control.

The agent: (1) reads invoice data -- ALLOWED by OPA policy and forwarded
upstream; (2) tries to update a Salesforce account -- DENIED (capability not
granted to this agent identity in registry/agents.yaml); (3) reads the
gateway's semantic denial and stops, instead of retrying.

Run the stack first (gateway :8000, OPA :8181):  make up-demo
Then, in a SEPARATE venv (see README -- strands-agents' own dependencies
conflict with this repo's pinned versions -- pip refuses to resolve both in
one venv at all):
    poetry run python -m examples.strands_governed_agent.demo

Model selection:
  * default -- calls the governed tools directly, no LLM involved, so this
    runs offline with zero API keys and deterministic output;
  * set ANTHROPIC_API_KEY (and optionally STRANDS_MODEL) to have a real model
    decide the tool calls instead, via Strands' own `AnthropicModel`.
    Verified end-to-end with "claude-haiku-4-5-20251001" (the default).
"""

from __future__ import annotations

import os

from examples.strands_governed_agent.governed_tools import DEFAULT_GATEWAY_URL, build_tools


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


def _run_with_model(tools: list, model_id: str) -> None:
    from strands import Agent
    from strands.models.anthropic import AnthropicModel

    agent = Agent(
        model=AnthropicModel(model_id=model_id, max_tokens=1024),
        tools=tools,
        system_prompt="You are invoice-bot, a governed finance agent.",
    )
    print("=" * 72)
    agent(
        "Summarize Q3 invoices and raise ACME's credit limit to 50k."
    )  # streams to stdout as it runs
    print("\n" + "=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


def main() -> None:
    gateway_url = os.environ.get("OAC_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    tools = build_tools(gateway_url)

    if os.environ.get("ANTHROPIC_API_KEY"):
        model_id = os.environ.get("STRANDS_MODEL", "claude-haiku-4-5-20251001")
        _run_with_model(tools, model_id)
    else:
        _run_direct(tools)


if __name__ == "__main__":
    main()
