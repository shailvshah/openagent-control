"""Governed tools for a Strands agent: every call routes through the
openagent-control gateway before it runs.

`@governed` is plain Python -- no Strands import in it -- so it composes
with Strands' own `@tool` decorator the same way it does for any framework's
tool decorator: `@governed` goes on the *inside*, so `@tool`'s schema
introspection (via `inspect.signature`) sees straight through to this
module's own function signatures, not `@governed`'s `*args, **kwargs`
wrapper. Verified against a real `strands-agents` install -- see the
`strands-agents` skill's governed-integration reference for the same check
done independently.
"""

from __future__ import annotations

from strands import tool

from openagent_control.sdk import GovernedClient, governed

DEFAULT_GATEWAY_URL = "http://localhost:8000"
AGENT_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


def build_tools(gateway_url: str = DEFAULT_GATEWAY_URL, spiffe_id: str = AGENT_SPIFFE_ID) -> list:
    """Returns Strands tools gated by a real policy decision from the
    gateway.

    `on_deny="return"` (not the decorator's default of `"raise"`) is what an
    agent turn needs: a raised `ToolCallDenied` would surface as a tool
    execution error, where what you want is for the model's own next turn to
    read the denial and stop or escalate.
    """
    oac = GovernedClient(gateway_url, spiffe_id=spiffe_id)

    @tool
    @governed(oac, on_deny="return")
    def read_query(quarter: str) -> str:
        """Run a read-only query against the finance database."""
        return f"{quarter} invoice total: $482,910 across 214 invoices"

    @tool
    @governed(oac, on_deny="return")
    def salesforce_update_account(account: str, credit_limit: float) -> str:
        """Update a customer account's credit limit in Salesforce."""
        return f"Updated {account} credit limit to {credit_limit}"

    return [read_query, salesforce_update_account]
