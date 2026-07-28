"""Governed tools for a CrewAI agent: every call routes through the
openagent-control gateway before it runs.

`@governed` is plain Python — no CrewAI import in it — so it composes with
CrewAI's own `@tool` decorator the same way it does for any framework's tool
decorator: `@governed` goes on the *inside*, so `@tool`'s schema introspection
(via `inspect.signature`) sees straight through to this module's own function
signatures, not `@governed`'s `*args, **kwargs` wrapper. Verified against a
real `crewai` install — see the `crewai` skill's governed-integration
reference for the same check done independently.
"""

from __future__ import annotations

from crewai.tools import tool

from openagent_control.sdk import GovernedClient, governed

DEFAULT_GATEWAY_URL = "http://localhost:8000"
AGENT_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


def build_tools(gateway_url: str = DEFAULT_GATEWAY_URL, spiffe_id: str = AGENT_SPIFFE_ID) -> list:
    """Returns CrewAI tools gated by a real policy decision from the gateway.

    `on_deny="return"` (not the decorator's default of `"raise"`) is what a
    Crew needs: a raised `ToolCallDenied` would propagate as a crashed task,
    where what you want is for the agent's own reasoning to read the denial
    and stop or escalate.
    """
    oac = GovernedClient(gateway_url, spiffe_id=spiffe_id)

    @tool("read_query")
    @governed(oac, on_deny="return")
    def read_query(quarter: str) -> str:
        """Run a read-only query against the finance database."""
        return f"{quarter} invoice total: $482,910 across 214 invoices"

    @tool("salesforce_update_account")
    @governed(oac, on_deny="return")
    def salesforce_update_account(account: str, credit_limit: float) -> str:
        """Update a customer account's credit limit in Salesforce."""
        return f"Updated {account} credit limit to {credit_limit}"

    return [read_query, salesforce_update_account]
