"""Client SDK: govern an agent's tool calls from inside the agent's own process.

Until this package existed, adopting `openagent-control` meant hand-writing
JSON-RPC over httpx by copying an example — a pip install shipped a gateway and
no client at all. It also meant your tools had to live behind an MCP server,
because proxying was the only integration pattern. That rules out most agents
already running in production, whose tool functions sit in their own process
and call Salesforce or a database directly.

Two integration shapes, both real (ADR-0017):

**Keep your code where it is** — the decorator asks the gateway for a decision
immediately before your function runs, and raises `ToolCallDenied` instead of
running it when policy says no:

    from openagent_control.sdk import GovernedClient, governed

    oac = GovernedClient("https://gateway.corp.net", token=agent_token)

    @governed(oac)
    def update_account(account_id: str, credit_limit: float) -> dict:
        return salesforce.update(account_id, credit_limit=credit_limit)

**Or proxy through the gateway** to tools that already live on an MCP server,
without writing the JSON-RPC envelope yourself:

    tools = oac.list_tools()               # only what this agent is granted
    result = oac.call_tool("read_query", {"quarter": "Q3"})

Both shapes are governed identically — same identity check, same policy engine,
same signed receipt — because both land in the same `GovernedExecutionService`.
The difference is only who runs the tool.

Sync and async are both first-class: `GovernedClient` and `AsyncGovernedClient`,
and `@governed` detects whether the function it wraps is a coroutine. Wrapping
an async tool with a blocking HTTP call underneath would stall the caller's
event loop, which is a real failure in an agent runtime, not a style point.
"""

from openagent_control.sdk.client import AsyncGovernedClient, Decision, GovernedClient
from openagent_control.sdk.decorator import ToolCallDenied, governed

__all__ = [
    "AsyncGovernedClient",
    "Decision",
    "GovernedClient",
    "ToolCallDenied",
    "governed",
]
