"""The SDK against a fully real stack (ADR-0017).

Real authorization server, real `opa` evaluating the real Rego, real downstream
MCP server over real SQLite, real gateway under uvicorn on a real socket — and
the SDK talking to it over real HTTP. This is the file that proves the claim
the SDK exists to make: an agent already running in production can gain
identity, policy and a signed receipt without moving its tool code anywhere.

The `@governed` tests are the important ones. A denial there means real OPA
evaluated the real policy against the real registry and the local Python
function did not run — which is the entire value proposition, end to end.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.authorization_server import (
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    run_authorization_server,
)
from examples.enterprise_scenario.harness import (
    AGENT_CLIENT_ID,
    GATEWAY_AUDIENCE,
    build_settings,
    run_gateway,
    run_opa,
)
from examples.enterprise_scenario.mcp_server import run_mcp_server

from openagent_control.sdk import GovernedClient, ToolCallDenied, governed
from openagent_control.sdk.client import ToolCallFailed

pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)


def _write_registry(path: Path, issuer: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "spiffe_id": f"oidc://{issuer}/{AGENT_CLIENT_ID}",
                        "display_name": "Finance Invoice Service",
                        "purpose": "Read and reconcile finance invoices.",
                        "owner": "alice@corp.net",
                        "risk_tier": "medium",
                        "status": "active",
                        # update_record is deliberately NOT granted.
                        "granted_tools": ["read_query"],
                    }
                ]
            }
        )
    )


@pytest.fixture(scope="module")
def oac(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GovernedClient]:
    registry = tmp_path_factory.mktemp("registry") / "agents.yaml"
    with (
        run_authorization_server(GATEWAY_AUDIENCE) as auth,
        run_opa() as opa_url,
        run_mcp_server(auth.issuer + "/keys", auth.issuer) as mcp_url,
    ):
        _write_registry(registry, auth.issuer)
        settings = build_settings(
            auth_discovery_url=auth.discovery_url,
            auth_token_url=auth.token_url,
            opa_url=opa_url,
            mcp_url=mcp_url,
            registry_path=registry,
            delegated_audience=mcp.AUDIENCE,
            client_id=GATEWAY_CLIENT_ID,
            client_secret=GATEWAY_CLIENT_SECRET,
        )
        with run_gateway(settings) as gateway_url:
            token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, None)
            with GovernedClient(gateway_url, token=token) as client:
                yield client


def test_authorize_allows_a_granted_tool_and_returns_a_signed_receipt(
    oac: GovernedClient,
) -> None:
    result = oac.authorize("read_query", {"quarter": "Q3"})

    assert result.allowed is True
    assert result.receipt_id


def test_authorize_denies_an_ungranted_tool_with_the_real_policys_reason(
    oac: GovernedClient,
) -> None:
    result = oac.authorize("update_record", {"invoice_id": "INV-1001", "status": "paid"})

    assert result.allowed is False
    assert "Capability not granted" in result.reason
    assert "request user approval" in result.instruction


def test_governed_runs_a_local_function_when_real_policy_allows(oac: GovernedClient) -> None:
    """The agent's own code, in the agent's own process — governed, not proxied."""
    ran: list[str] = []

    @governed(oac)
    def read_query(quarter: str) -> str:
        ran.append(quarter)
        return f"local result for {quarter}"

    assert read_query("Q3") == "local result for Q3"
    assert ran == ["Q3"]


def test_governed_blocks_a_local_function_when_real_policy_denies(oac: GovernedClient) -> None:
    """The whole proposition in one test: real OPA evaluated the real policy
    against the real registry, and the local side effect never happened."""
    ran: list[str] = []

    @governed(oac)
    def update_record(invoice_id: str, status: str) -> str:
        ran.append(invoice_id)
        return "written"

    with pytest.raises(ToolCallDenied, match="Capability not granted"):
        update_record("INV-1001", "written_off")

    assert ran == []


def test_call_tool_reaches_the_real_downstream_server(oac: GovernedClient) -> None:
    result = oac.call_tool("read_query", {"quarter": "Q3"})

    rows = result["structuredContent"]["rows"]
    assert [r["invoice_id"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]


def test_a_denied_proxy_call_carries_the_stop_instruction(oac: GovernedClient) -> None:
    with pytest.raises(ToolCallFailed, match="request user approval"):
        oac.call_tool("update_record", {"invoice_id": "INV-1001", "status": "paid"})


def test_list_tools_shows_only_the_granted_tool(oac: GovernedClient) -> None:
    """The real downstream advertises update_record too; the registry grant is
    what the agent is allowed to see (ADR-0016)."""
    names = {tool["name"] for tool in oac.list_tools()}

    assert names == {"read_query"}


# --- LangChain / LangGraph integration -------------------------------------
#
# Against real LangChain (CI installs it via --with examples), because the
# whole risk here is how a real framework introspects a decorated function —
# the tool name, argument schema and description it infers. A fake tool
# decorator would agree with whatever this project assumed and prove nothing.


def test_govern_produces_a_real_langchain_tool_with_the_right_schema(
    oac: GovernedClient,
) -> None:
    """LangChain infers name, args schema and description from the function.
    @governed must be transparent to that, or the model is told the wrong thing
    about a tool it is about to call."""
    lc = pytest.importorskip("openagent_control.sdk.langchain")

    def read_query(quarter: str) -> str:
        """Read invoice rows for a quarter."""
        return f"rows for {quarter}"

    tool = lc.govern(read_query, oac)

    assert tool.name == "read_query"
    assert tool.description == "Read invoice rows for a quarter."
    assert set(tool.args) == {"quarter"}


def test_an_allowed_langchain_tool_invokes_the_underlying_function(
    oac: GovernedClient,
) -> None:
    lc = pytest.importorskip("openagent_control.sdk.langchain")

    def read_query(quarter: str) -> str:
        """Read invoice rows for a quarter."""
        return f"rows for {quarter}"

    result = lc.govern(read_query, oac).invoke({"quarter": "Q3"})

    assert result == "rows for Q3"


def test_a_denied_langchain_tool_returns_text_rather_than_raising(
    oac: GovernedClient,
) -> None:
    """An exception here propagates out of the tool node and ends the run. The
    model must instead read the denial and stop on its own."""
    lc = pytest.importorskip("openagent_control.sdk.langchain")
    ran: list[str] = []

    def update_record(invoice_id: str, status: str) -> str:
        """Update an invoice's status."""
        ran.append(invoice_id)
        return "written"

    result = lc.govern(update_record, oac).invoke(
        {"invoice_id": "INV-1001", "status": "written_off"}
    )

    assert result.startswith("BLOCKED:")
    assert "request user approval" in result
    assert ran == []


def test_proxied_tools_exposes_only_the_granted_tool(oac: GovernedClient) -> None:
    """Tools that live on the MCP server, as LangChain tools — built from the
    gateway's filtered listing, so the model never sees an ungranted one."""
    lc = pytest.importorskip("openagent_control.sdk.langchain")

    tools = lc.proxied_tools(oac)

    assert [t.name for t in tools] == ["read_query"]
    # The argument schema comes from the MCP tool's own inputSchema — without it
    # LangChain has nothing to introspect on the proxy and sends no arguments,
    # which the upstream rejects as a missing required field.
    assert set(tools[0].args) == {"quarter"}
    assert "INV-1001" in tools[0].invoke({"quarter": "Q3"})


def test_governed_tools_run_inside_a_real_langgraph_graph(oac: GovernedClient) -> None:
    """LangGraph executes tools through a ToolNode inside a compiled graph, not
    by calling them directly. A denial must arrive there as a ToolMessage the
    model can read — surfacing as an exception would end the run instead of
    letting the agent stop, which is the opposite of the intended behaviour.
    """
    lc = pytest.importorskip("openagent_control.sdk.langchain")
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    def read_query(quarter: str) -> str:
        """Read invoice rows for a quarter."""
        return f"rows for {quarter}"

    def update_record(invoice_id: str, status: str) -> str:
        """Update an invoice's status."""
        return "written"

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([lc.govern(read_query, oac), lc.govern(update_record, oac)]))
    graph.set_entry_point("tools")
    graph.add_edge("tools", END)
    compiled = graph.compile()

    call = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_query", "args": {"quarter": "Q3"}, "id": "1", "type": "tool_call"},
            {
                "name": "update_record",
                "args": {"invoice_id": "INV-1001", "status": "written_off"},
                "id": "2",
                "type": "tool_call",
            },
        ],
    )

    messages = compiled.invoke({"messages": [call]})["messages"]
    by_id = {m.tool_call_id: m.content for m in messages if hasattr(m, "tool_call_id")}

    assert by_id["1"] == "rows for Q3"
    assert by_id["2"].startswith("BLOCKED:")
    assert "request user approval" in by_id["2"]
