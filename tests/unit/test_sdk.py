"""SDK client and @governed decorator.

The gateway here is the real `create_app()` — real routes, real
GovernedExecutionService, real registry gate, real ledger — served to httpx
over ASGI rather than a socket. Only the policy engine and the upstream are
substituted, because a real OPA needs a binary the unit suite must not require.

httpx's ASGITransport is async-only, so this file drives `AsyncGovernedClient`
for everything HTTP-shaped. The sync `GovernedClient` is proven against a real
gateway over a real socket, with real OPA, in
tests/integration/test_sdk_end_to_end.py — not skipped, just somewhere it can
have a real server to talk to.
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.models import Decision as DomainDecision
from openagent_control.domain.models import PolicyDecision, ToolCallRequest
from openagent_control.gateway.app import create_app
from openagent_control.gateway.dependencies import Container
from openagent_control.sdk import AsyncGovernedClient, GovernedClient, ToolCallDenied, governed
from openagent_control.sdk.client import (
    AuthorizationResult,
    Decision,
    GatewayError,
    ToolCallFailed,
)

AGENT = "spiffe://corp.net/ns/finance/agent/invoice-bot"
UNREGISTERED = "spiffe://corp.net/ns/finance/agent/never-registered"


class _Policy:
    def __init__(self, decision: PolicyDecision) -> None:
        self._decision = decision

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        # Mirrors the real Rego contract: tools/list is always allowed, and only
        # tools/call is subject to the configured decision.
        if request.method == "tools/list":
            return PolicyDecision(decision=DomainDecision.ALLOW)
        return self._decision


class _Upstream:
    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        if request.method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {
                    "tools": [
                        {"name": "read_query", "description": "Read invoices."},
                        {"name": "update_record", "description": "Update an invoice."},
                        {"name": "not_granted", "description": "Should never be listed."},
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "result": {"content": [{"type": "text", "text": "upstream ran it"}]},
        }


def _app(decision: PolicyDecision) -> Any:
    app = create_app()
    app.state.container = Container(
        identity_provider=HeaderIdentityProvider(),
        agent_registry=FileAgentRegistry("registry/agents.yaml"),
        policy_engine=_Policy(decision),
        ledger=Ed25519ChainLedger(),
        audit_exporter=StdoutAuditExporter(),
        token_exchange=StubTokenExchange(),
        mcp_upstream=_Upstream(),
    )
    return app


def _client(
    decision: PolicyDecision, spiffe_id: str | None = AGENT, token: str | None = None
) -> AsyncGovernedClient:
    headers = {"X-Spiffe-ID": spiffe_id} if spiffe_id else {}
    return AsyncGovernedClient(
        "http://gateway.test",
        spiffe_id=spiffe_id,
        token=token,
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(decision)),
            headers=headers,
            base_url="http://gateway.test",
        ),
    )


_ALLOW = PolicyDecision(decision=DomainDecision.ALLOW)
_DENY = PolicyDecision(decision=DomainDecision.DENY, reason="Capability not granted")


# --- authorize, against the real gateway -----------------------------------


@pytest.mark.asyncio
async def test_authorize_returns_a_real_receipt_for_an_allowed_call() -> None:
    """The receipt id is the point: an allowed call is recorded and signed, not
    merely permitted, so the decision is auditable after the fact."""
    result = await _client(_ALLOW).authorize("read_query", {"quarter": "Q3"})

    assert result.allowed is True
    assert result.decision is Decision.ALLOW
    assert result.receipt_id


@pytest.mark.asyncio
async def test_authorize_reports_a_denial_with_its_reason_and_instruction() -> None:
    result = await _client(_DENY).authorize("update_record", {})

    assert result.allowed is False
    assert "Capability not granted" in result.reason
    assert "request user approval" in result.instruction


@pytest.mark.asyncio
async def test_an_unregistered_agent_is_refused_by_the_registry_gate() -> None:
    """An agent the registry has never heard of is denied and receipted, so the
    SDK must surface that as a denial rather than an obscure transport error."""
    result = await _client(_ALLOW, spiffe_id=UNREGISTERED).authorize("read_query")

    assert result.allowed is False
    assert "not registered" in result.reason


@pytest.mark.asyncio
async def test_a_missing_identity_is_a_gateway_error_not_a_denial() -> None:
    """401 and DENY are different facts: one means "we don't know who you are",
    the other "we know, and no". Collapsing them would hide misconfiguration."""
    oac = _client(_ALLOW, spiffe_id=None, token="not-a-real-token")

    with pytest.raises(GatewayError, match="refused the agent's identity"):
        await oac.authorize("read_query")


def test_an_unreachable_gateway_is_an_error_never_a_silent_allow() -> None:
    """Fail-open here would make an outage indistinguishable from permission."""
    oac = GovernedClient("http://127.0.0.1:1", spiffe_id=AGENT, timeout=0.5)

    with pytest.raises(GatewayError, match="could not reach the gateway"):
        oac.authorize("read_query")


def test_a_client_with_no_credential_at_all_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="either token="):
        GovernedClient("http://gateway.test")


# --- proxy shape, against the real gateway ---------------------------------


@pytest.mark.asyncio
async def test_call_tool_proxies_through_the_gateway_to_the_upstream() -> None:
    result = await _client(_ALLOW).call_tool("read_query", {"quarter": "Q3"})

    assert result["content"][0]["text"] == "upstream ran it"


@pytest.mark.asyncio
async def test_a_denied_proxy_call_raises_with_the_gateways_instruction() -> None:
    with pytest.raises(ToolCallFailed, match="request user approval"):
        await _client(_DENY).call_tool("update_record", {})


@pytest.mark.asyncio
async def test_list_tools_returns_only_what_the_registry_grants() -> None:
    """The upstream advertises `not_granted`; the registry does not grant it,
    so the agent must never see it (ADR-0016)."""
    names = {tool["name"] for tool in await _client(_ALLOW).list_tools()}

    assert names == {"read_query", "update_record"}


# --- @governed, async path against the real gateway ------------------------


@pytest.mark.asyncio
async def test_an_allowed_call_runs_the_real_function() -> None:
    oac = _client(_ALLOW)
    calls: list[float] = []

    @governed(oac)
    async def update_account(account_id: str, credit_limit: float) -> str:
        calls.append(credit_limit)
        return f"updated {account_id}"

    assert await update_account("ACC-1", 5_000) == "updated ACC-1"
    assert calls == [5_000]


@pytest.mark.asyncio
async def test_a_denied_call_never_runs_the_function_body() -> None:
    """The whole point of the decorator: the side effect must not happen."""
    oac = _client(_DENY)
    ran: list[str] = []

    @governed(oac)
    async def update_record() -> str:
        ran.append("boom")
        return "done"

    with pytest.raises(ToolCallDenied, match="denied by policy"):
        await update_record()
    assert ran == []


@pytest.mark.asyncio
async def test_on_deny_return_yields_agent_readable_text_instead_of_raising() -> None:
    """Inside an agent loop an exception ends the run; the model needs to read
    the denial and stop on its own."""
    oac = _client(_DENY)

    @governed(oac, on_deny="return")
    async def update_record() -> str:
        return "ok"

    result = await update_record()

    assert result.startswith("BLOCKED:")
    assert "request user approval" in result


# --- @governed, sync path --------------------------------------------------
#
# No in-process transport can back a sync httpx.Client, so these use a canned
# decision. They cover what the decorator itself decides — argument binding,
# denial handling, metadata preservation — while the sync client's real HTTP
# behaviour is covered end-to-end in tests/integration/test_sdk_end_to_end.py.


class _StubClient(GovernedClient):
    def __init__(self, result: AuthorizationResult) -> None:
        super().__init__("http://gateway.test", spiffe_id=AGENT)
        self._result = result
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def authorize(self, tool: str, arguments: dict[str, Any] | None = None) -> AuthorizationResult:
        self.seen.append((tool, arguments or {}))
        return self._result


_ALLOWED = AuthorizationResult(
    allowed=True, decision=Decision.ALLOW, reason="", instruction="", receipt_id="r-1"
)
_DENIED = AuthorizationResult(
    allowed=False,
    decision=Decision.DENY,
    reason="Capability not granted",
    instruction="Stop execution and request user approval.",
    receipt_id="r-2",
)


def test_a_denied_sync_call_never_runs_the_function_body() -> None:
    oac = _StubClient(_DENIED)
    ran: list[str] = []

    @governed(oac)
    def delete_everything() -> str:
        ran.append("boom")
        return "done"

    with pytest.raises(ToolCallDenied, match="denied by policy"):
        delete_everything()
    assert ran == []


def test_positional_arguments_are_evaluated_by_name() -> None:
    """A guardrail on `credit_limit` must see the value whether it arrived
    positionally or as a keyword — otherwise call style alone bypasses policy."""
    oac = _StubClient(_ALLOWED)

    @governed(oac)
    def update_account(account_id: str, credit_limit: float = 0.0) -> str:
        return "ok"

    update_account("ACC-1", 50_000)

    assert oac.seen == [("update_account", {"account_id": "ACC-1", "credit_limit": 50_000})]


def test_defaults_are_included_in_what_policy_evaluates() -> None:
    """A threshold rule must see the value that will actually be used, not an
    absent key, when the caller relied on the default."""
    oac = _StubClient(_ALLOWED)

    @governed(oac)
    def update_account(account_id: str, credit_limit: float = 100.0) -> str:
        return "ok"

    update_account("ACC-1")

    assert oac.seen == [("update_account", {"account_id": "ACC-1", "credit_limit": 100.0})]


def test_the_tool_name_defaults_to_the_function_name() -> None:
    oac = _StubClient(_DENIED)

    @governed(oac)
    def update_record() -> str:
        return "ok"

    with pytest.raises(ToolCallDenied, match="'update_record'"):
        update_record()


def test_an_explicit_name_overrides_the_function_name() -> None:
    oac = _StubClient(_DENIED)

    @governed(oac, name="update_record")
    def some_internal_helper() -> str:
        return "ok"

    with pytest.raises(ToolCallDenied, match="'update_record'"):
        some_internal_helper()


def test_the_decorator_preserves_signature_and_docstring() -> None:
    """Agent frameworks infer a tool's schema and description from these; losing
    them would silently change what the model is told the tool does."""
    oac = _StubClient(_ALLOWED)

    @governed(oac)
    def read_query(quarter: str) -> str:
        """Read invoices for a quarter."""
        return "ok"

    assert read_query.__name__ == "read_query"
    assert read_query.__doc__ == "Read invoices for a quarter."
    assert list(inspect.signature(read_query).parameters) == ["quarter"]


def test_a_call_that_does_not_match_the_signature_still_reaches_the_function() -> None:
    """Binding failure must not become a governance error — the function is
    about to raise the real, clearer TypeError itself."""
    oac = _StubClient(_ALLOWED)

    @governed(oac)
    def read_query(quarter: str) -> str:
        return "ok"

    with pytest.raises(TypeError):
        read_query()  # type: ignore[call-arg]


def test_wrapping_a_non_function_explains_the_decorator_order() -> None:
    oac = _StubClient(_ALLOWED)

    with pytest.raises(TypeError, match="must be the INNER"):
        governed(oac)("not a function")  # type: ignore[type-var]


def test_a_sync_function_with_an_async_client_is_refused() -> None:
    oac = AsyncGovernedClient("http://gateway.test", spiffe_id=AGENT)

    with pytest.raises(TypeError, match="needs a GovernedClient"):

        @governed(oac)
        def read_query() -> str:
            return "ok"


def test_an_async_function_with_a_sync_client_is_refused() -> None:
    """A blocking authorize() inside a coroutine stalls the whole event loop —
    a real failure under load, so it is refused at decoration time."""
    oac = _StubClient(_ALLOWED)

    with pytest.raises(TypeError, match="needs an AsyncGovernedClient"):

        @governed(oac)
        async def read_query() -> str:
            return "ok"


# --- transport and error-shape edges ---------------------------------------


def test_a_delegated_call_sends_the_human_sponsors_subject_token() -> None:
    """Delegated (on-behalf-of) calls need the sponsor's token; without it the
    gateway refuses rather than silently downgrading to an autonomous call."""
    oac = GovernedClient("http://gateway.test", token="agent-token", subject_token="sponsor-token")

    assert oac._headers["X-Subject-Token"] == "sponsor-token"  # noqa: SLF001
    assert oac._headers["Authorization"] == "Bearer agent-token"  # noqa: SLF001


def _erroring_client(status: int, body: Any, text: str = "") -> GovernedClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return GovernedClient(
        "http://gateway.test",
        spiffe_id=AGENT,
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gateway.test"),
    )


def test_a_server_error_is_reported_with_the_gateways_own_detail() -> None:
    oac = _erroring_client(500, {"detail": "database is down"})

    with pytest.raises(GatewayError, match="database is down"):
        oac.authorize("read_query")


def test_a_non_json_error_body_still_produces_a_readable_message() -> None:
    """An HTML error page from a proxy in front of the gateway must not turn
    into a JSONDecodeError that hides what actually happened."""
    oac = _erroring_client(502, None, text="<html>Bad Gateway</html>")

    with pytest.raises(GatewayError, match="Bad Gateway"):
        oac.authorize("read_query")


def test_an_error_body_with_no_recognised_field_is_still_surfaced() -> None:
    oac = _erroring_client(503, {"unexpected": "shape"})

    with pytest.raises(GatewayError, match="unexpected"):
        oac.authorize("read_query")


def test_an_unreachable_gateway_fails_call_tool_and_list_tools_too() -> None:
    """All three methods must fail closed, not just authorize()."""
    oac = GovernedClient("http://127.0.0.1:1", spiffe_id=AGENT, timeout=0.5)

    with pytest.raises(GatewayError):
        oac.call_tool("read_query")
    with pytest.raises(GatewayError):
        oac.list_tools()


def test_the_sync_client_closes_cleanly_as_a_context_manager() -> None:
    with GovernedClient("http://gateway.test", spiffe_id=AGENT) as oac:
        assert oac is not None


@pytest.mark.asyncio
async def test_the_async_client_closes_cleanly_as_a_context_manager() -> None:
    async with AsyncGovernedClient("http://gateway.test", spiffe_id=AGENT) as oac:
        assert oac is not None


@pytest.mark.asyncio
async def test_an_unreachable_gateway_fails_every_async_method() -> None:
    oac = AsyncGovernedClient("http://127.0.0.1:1", spiffe_id=AGENT, timeout=0.5)

    with pytest.raises(GatewayError):
        await oac.authorize("read_query")
    with pytest.raises(GatewayError):
        await oac.call_tool("read_query")
    with pytest.raises(GatewayError):
        await oac.list_tools()
    await oac.aclose()
