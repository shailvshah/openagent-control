"""Day-1 demo: a CrewAI agent whose every tool call is governed by
openagent-control.

The agent: (1) reads invoice data -- ALLOWED by OPA policy and forwarded
upstream; (2) tries to update a Salesforce account -- DENIED (capability not
granted to this agent identity in registry/agents.yaml); (3) reads the
gateway's semantic denial and stops, instead of retrying.

Run the stack first (gateway :8000, OPA :8181):  make up-demo
Then, in a SEPARATE venv (see README -- crewai's own dependencies conflict
with this repo's pinned versions):
    poetry run python -m examples.crewai_governed_agent.demo

Model selection:
  * default -- calls the governed tools directly, no LLM involved, so this
    runs offline with zero API keys and deterministic output;
  * set ANTHROPIC_API_KEY (and optionally CREWAI_MODEL) to have a real model
    decide the tool calls instead. Verified end-to-end with
    "anthropic/claude-haiku-4-5-20251001" (the default). CrewAI's *native*
    Anthropic provider (crewai[anthropic]'s completion path, not litellm)
    sends an assistant-message-prefill the newer claude-sonnet-4-6 API
    rejects outright ("This model does not support assistant message
    prefill") -- a CrewAI/model compatibility quirk, not anything on the
    gateway side; if you hit that error switching CREWAI_MODEL, it's this.
"""

from __future__ import annotations

import os

from crewai import Agent, Crew, Process, Task
from examples.crewai_governed_agent.governed_tools import DEFAULT_GATEWAY_URL, build_tools


def _run_direct(tools: list) -> None:
    """Zero-API-key path: call the governed tools directly, proving the real
    ALLOW/DENY decisions and receipts without needing a live model to decide
    to call them."""
    read_query, salesforce_update_account = tools
    print("=" * 72)
    print("[direct] read_query(quarter='Q3') ->")
    print(" ", read_query.run(quarter="Q3"))
    print("[direct] salesforce_update_account(account='ACME', credit_limit=50000) ->")
    print(" ", salesforce_update_account.run(account="ACME", credit_limit=50000))
    print("=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


def _run_with_model(tools: list, model_id: str) -> None:
    from crewai import LLM

    analyst = Agent(
        role="Finance Analyst",
        goal="Summarize invoices and act on account update requests",
        backstory="You are invoice-bot, a governed finance agent.",
        tools=tools,
        llm=LLM(model=model_id),
    )
    task = Task(
        description="Summarize Q3 invoices and raise ACME's credit limit to 50k.",
        expected_output="A summary of what was done and what was blocked, if anything.",
        agent=analyst,
    )
    crew = Crew(agents=[analyst], tasks=[task], process=Process.sequential)
    result = crew.kickoff()
    print("=" * 72)
    print(result)
    print("=" * 72)
    print(
        "Every call above -- including the blocked one -- produced a signed, "
        "hash-chained audit receipt in the gateway logs (docker compose logs gateway)."
    )


def main() -> None:
    gateway_url = os.environ.get("OAC_GATEWAY_URL", DEFAULT_GATEWAY_URL)
    tools = build_tools(gateway_url)

    if os.environ.get("ANTHROPIC_API_KEY"):
        model_id = os.environ.get("CREWAI_MODEL", "anthropic/claude-haiku-4-5-20251001")
        _run_with_model(tools, model_id)
    else:
        _run_direct(tools)


if __name__ == "__main__":
    main()
