"""Governed tools for a Google ADK agent: every call routes through the
openagent-control gateway before it runs.

ADK needs no tool decorator at all -- plain functions go straight into
`Agent(tools=[...])`, and ADK wraps each one in a `FunctionTool` internally,
deriving the function-calling schema from the function's own type hints and
docstring. `@governed` (plain Python, no ADK import) sits directly on the
function; `functools.wraps` preserves the signature ADK's schema builder
needs. Verified against a real `google-adk` install -- see the `google-adk`
skill's governed-integration reference for the same check done
independently.
"""

from __future__ import annotations

from openagent_control.sdk import GovernedClient, governed

DEFAULT_GATEWAY_URL = "http://localhost:8000"
AGENT_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


def build_tools(gateway_url: str = DEFAULT_GATEWAY_URL, spiffe_id: str = AGENT_SPIFFE_ID) -> list:
    """Returns plain functions gated by a real policy decision from the
    gateway, ready to hand to `Agent(tools=...)`.

    `on_deny="return"` (not the decorator's default of `"raise"`) is what an
    agent turn needs: a raised `ToolCallDenied` would surface as a tool
    execution error, where what you want is for the model's own next turn to
    read the denial and stop or escalate.
    """
    oac = GovernedClient(gateway_url, spiffe_id=spiffe_id)

    @governed(oac, on_deny="return")
    def read_query(quarter: str) -> str:
        """Run a read-only query against the finance database."""
        return f"{quarter} invoice total: $482,910 across 214 invoices"

    @governed(oac, on_deny="return")
    def salesforce_update_account(account: str, credit_limit: float) -> str:
        """Update a customer account's credit limit in Salesforce."""
        return f"Updated {account} credit limit to {credit_limit}"

    return [read_query, salesforce_update_account]
