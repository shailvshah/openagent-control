"""The governed-execution use case: the transport-agnostic core of the gateway.

Every deployment pattern from ADR-0001 (egress gateway today; sidecar and native SDK
later) drives this same service rather than re-implementing the sequence:

    identify -> evaluate policy -> record + export receipt
             -> (deny: semantic error) | (allow: broker credential, forward upstream)

Failure posture:
- Policy engine unreachable  => fail closed: an explicit DENY is recorded and
  exported, so the outage itself leaves an audit trail (never a silent 500).
- Upstream failure after ALLOW => JSON-RPC error with a stop instruction, so an LLM
  agent halts instead of retry-looping; the ALLOW receipt has already been recorded.
- Identity / missing subject token => typed errors for the transport layer to map
  (HTTP 401 in the FastAPI adapter).
"""

from __future__ import annotations

from typing import Any, Literal

from opentelemetry import trace

from openagent_control.domain.errors import (
    IdentityError,
    MissingSubjectTokenError,
    PolicyEngineUnavailableError,
    TokenExchangeError,
    UpstreamError,
)
from openagent_control.domain.models import (
    AgentIdentity,
    AgentStatus,
    AuthorizationOutcome,
    Decision,
    PolicyDecision,
    SubjectIdentity,
    ToolCallRequest,
)
from openagent_control.domain.ports import (
    AgentRegistry,
    AuditExporter,
    IdentityProvider,
    Ledger,
    MCPUpstream,
    PolicyEngine,
    SubjectVerifier,
    TokenExchange,
)

_SUBJECT_TOKEN_HEADER = "x-subject-token"

_STOP_INSTRUCTION = "Stop execution and request user approval."
_FAIL_CLOSED_REASON = "Policy engine unavailable; denied (fail-closed)"

# Always pulled from the API, never the SDK: with no TracerProvider configured
# (tracing.configure_tracing() is opt-in, see that module) this resolves to a
# no-op tracer, so instrumentation is unconditional and free to leave in place.
_tracer = trace.get_tracer("openagent_control.governed_execution")


class GovernedExecutionService:
    def __init__(
        self,
        *,
        identity_provider: IdentityProvider,
        agent_registry: AgentRegistry,
        policy_engine: PolicyEngine,
        ledger: Ledger,
        audit_exporter: AuditExporter,
        token_exchange: TokenExchange,
        mcp_upstream: MCPUpstream,
        delegated_audience: str,
        decision_mode: Literal["enforce", "observe"] = "enforce",
        subject_verifier: SubjectVerifier | None = None,
        subject_binding: Literal["strict", "may-act-only", "off"] = "strict",
    ) -> None:
        self._identity_provider = identity_provider
        self._agent_registry = agent_registry
        self._policy_engine = policy_engine
        self._ledger = ledger
        self._audit_exporter = audit_exporter
        self._token_exchange = token_exchange
        self._mcp_upstream = mcp_upstream
        self._delegated_audience = delegated_audience
        self._decision_mode = decision_mode
        self._subject_verifier = subject_verifier
        self._subject_binding = subject_binding

    async def authorize(
        self, headers: dict[str, str], payload: dict[str, Any]
    ) -> AuthorizationOutcome:
        """Decides and receipts one tool call *without* executing it.

        The native-SDK pattern of ADR-0001: an agent whose tool functions
        already exist and already do real work does not want its logic moved
        behind a proxy — it wants the identity check, the policy decision, and
        the signed receipt to happen immediately before its own code runs. That
        is exactly `execute()` minus the credential brokering and the forward,
        so this is the shared half rather than a second copy of the security
        sequence; `execute()` calls it too.

        Raises IdentityError or MissingSubjectTokenError for the transport layer
        to map to its own auth failure shape.
        """
        with _tracer.start_as_current_span("governed_execution.authorize") as root_span:
            return await self._authorize(headers, payload, root_span)

    async def execute(self, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        """Runs one governed tool call; returns a JSON-RPC response object.

        Raises IdentityError or MissingSubjectTokenError for the transport layer
        to map to its own auth failure shape.
        """
        with _tracer.start_as_current_span("governed_execution.execute") as root_span:
            outcome = await self._authorize(headers, payload, root_span)
            call = outcome.call

            if not outcome.allowed:
                return _jsonrpc_error(
                    call.request_id,
                    code=-32000,
                    message=f"Policy violation: {outcome.decision.reason}",
                    instruction=_STOP_INSTRUCTION,
                )

            with _tracer.start_as_current_span("broker_credential"):
                try:
                    credential = await self._broker_credential(call.agent, headers)
                except TokenExchangeError as exc:
                    return _jsonrpc_error(
                        call.request_id,
                        code=-32004,
                        message=f"Credential brokering failed: {exc}",
                        instruction="Do not retry automatically; report the failure to the user.",
                    )

            with _tracer.start_as_current_span("forward"):
                try:
                    response = await self._mcp_upstream.forward(call, credential)
                    return _filter_listing(response, call)
                except UpstreamError as exc:
                    return _jsonrpc_error(
                        call.request_id,
                        code=-32002,
                        message=f"Upstream execution failed: {exc}",
                        instruction="Do not retry automatically; report the failure to the user.",
                    )

    async def _authorize(
        self, headers: dict[str, str], payload: dict[str, Any], root_span: trace.Span
    ) -> AuthorizationOutcome:
        with _tracer.start_as_current_span("identify"):
            agent = await self._identity_provider.identify(headers)
        root_span.set_attribute("agent.spiffe_id", agent.spiffe_id)

        with _tracer.start_as_current_span("registry.lookup"):
            registration = await self._agent_registry.lookup(agent.spiffe_id)

        params = payload.get("params") or {}
        call = ToolCallRequest(
            method=payload.get("method", ""),
            tool_name=params.get("name"),
            arguments=params.get("arguments", {}),
            agent=agent,
            registration=registration,
            request_id=payload.get("id"),
        )
        root_span.set_attribute("rpc.method", call.method)
        if call.tool_name:
            root_span.set_attribute("tool.name", call.tool_name)

        # The user's own entitlements, resolved before policy so a rule can
        # reason about them (ADR-0019). Sponsorship is an approval; this is
        # the authorization principal, and only this is verified.
        #
        # Triggered by the *subject token's presence*, not `agent.human_sponsor`
        # (ADR-0020): a stable, autonomous agent identity (client_credentials,
        # no sponsor claim on its own token) serving many end-users still needs
        # per-request subject verification when boundary 1 attaches a different
        # human's token to each call — `human_sponsor` being unset on the
        # agent's own token doesn't mean the call isn't delegated, only that
        # the agent didn't re-authenticate itself per user. `_bind_subject`
        # already treats an unset `human_sponsor` as "nothing to bind against"
        # rather than a mismatch, so this doesn't weaken the binding check.
        normalized_headers = {k.lower(): v for k, v in headers.items()}
        subject_token = normalized_headers.get(_SUBJECT_TOKEN_HEADER, "")
        if self._subject_verifier is not None and subject_token:
            with _tracer.start_as_current_span("verify_subject"):
                subject = await self._subject_verifier.verify(subject_token)
                _bind_subject(agent, subject, self._subject_binding)
                call.subject = subject
            root_span.set_attribute("subject.id", subject.subject_id)

        # Registry gate (ADR-0008): orphaned or suspended agents never reach
        # the policy engine, but the attempt is still receipted below. This
        # is a hard security boundary, not a policy call —
        # decision_mode="observe" never softens it (ADR-0012).
        shadowable = True
        with _tracer.start_as_current_span("policy_evaluate") as policy_span:
            if registration is None:
                decision = PolicyDecision(
                    decision=Decision.DENY,
                    reason="Agent not registered in the Agent Registry "
                    "(orphaned agents are refused)",
                )
                shadowable = False
            elif registration.status is not AgentStatus.ACTIVE:
                decision = PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"Agent is {registration.status.value} in the Agent Registry",
                )
                shadowable = False
            else:
                try:
                    decision = await self._policy_engine.evaluate(call)
                except PolicyEngineUnavailableError:
                    # An outage of the policy engine is an infrastructure
                    # failure, not a policy signal shadow mode exists to
                    # observe — always enforced, same as the registry gate.
                    decision = PolicyDecision(decision=Decision.DENY, reason=_FAIL_CLOSED_REASON)
                    shadowable = False
            policy_span.set_attribute("policy.decision", decision.decision.value)

        # Only an explicit OPA DENY can be shadowed: recorded and signed as
        # a real DENY, but not actually blocked, so a first deployment can
        # watch what a policy WOULD reject before it starts rejecting
        # anything.
        shadow = (
            shadowable
            and self._decision_mode == "observe"
            and decision.decision is not Decision.ALLOW
        )
        root_span.set_attribute("policy.decision", decision.decision.value)
        root_span.set_attribute("policy.shadowed", shadow)

        receipt = await self._ledger.record(agent, call, decision, enforced=not shadow)
        await self._audit_exporter.export(receipt)

        return AuthorizationOutcome(
            allowed=decision.decision is Decision.ALLOW or shadow,
            decision=decision,
            receipt=receipt,
            call=call,
            shadowed=shadow,
        )

    async def _broker_credential(self, agent: AgentIdentity, headers: dict[str, str]) -> str:
        """Obtains the short-lived, audience-scoped credential for the upstream.

        Delegated calls exchange the human sponsor's subject token. Autonomous
        calls exchange the agent's own access token — RFC 8693 permits either,
        and a real resource server validates the result's audience and scope, so
        the agent must never be handed a credential it could reuse elsewhere.

        The placeholder below is reachable only when no bearer token exists at
        all, which means identity_mode="header" — the dev stub of ADR-0005,
        where there is no real credential to broker in the first place.
        """
        normalized = {k.lower(): v for k, v in headers.items()}

        if agent.human_sponsor:
            subject_token = normalized.get(_SUBJECT_TOKEN_HEADER)
            if not subject_token:
                raise MissingSubjectTokenError(
                    f"delegated call for sponsor '{agent.human_sponsor}' "
                    f"requires a '{_SUBJECT_TOKEN_HEADER}' header"
                )
            return await self._token_exchange.exchange(subject_token, self._delegated_audience)

        scheme, _, agent_token = normalized.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not agent_token:
            return f"autonomous::{agent.spiffe_id}"
        return await self._token_exchange.exchange(agent_token, self._delegated_audience)


def _bind_subject(
    agent: AgentIdentity, subject: SubjectIdentity, mode: Literal["strict", "may-act-only", "off"]
) -> None:
    """Checks that the verified user actually authorized *this* agent.

    Without a binding, an agent holding any user's valid subject token could
    present it while claiming a different sponsor: the IdP would mint a real
    credential for that user, and the receipt would attribute the call to
    someone else. The IdP rejects forged tokens, so this is not about forgery —
    it is about attribution and blast radius.

    Two mechanisms, preferred in order:

    1. **RFC 8693 `may_act`** — the spec's own way for a subject token to name
       the party authorized to act for that user. When the IdP issues it, it is
       authoritative and is checked against the agent's client id.
    2. **Issuer-scoped subject equality** — a fallback for the many IdPs that
       do not issue `may_act` at all.

    The fallback is skippable (`may-act-only`) because it is *not* universally
    sound: with pairwise subject identifiers, the same human legitimately has a
    different `sub` in the agent's token than in the subject token, since the
    value is derived per client. Enforcing equality there would reject valid
    delegated calls, so an operator on a pairwise tenant needs a way out that
    isn't "turn binding off entirely".
    """
    if mode == "off":
        return

    if subject.authorized_actor is not None:
        if agent.client_id and subject.authorized_actor != agent.client_id:
            raise IdentityError(
                f"subject token authorizes '{subject.authorized_actor}' to act, "
                f"but the caller is '{agent.client_id}' (RFC 8693 may_act mismatch)"
            )
        return

    if mode == "may-act-only":
        return

    if agent.human_sponsor and subject.subject_id != agent.human_sponsor:
        raise IdentityError(
            "subject token does not belong to the sponsor this call claims "
            f"('{agent.human_sponsor}'). If this IdP uses pairwise subject "
            "identifiers, set OAC_SUBJECT_BINDING=may-act-only."
        )


def _filter_listing(response: dict[str, Any], call: ToolCallRequest) -> dict[str, Any]:
    """Projects a tools/list response down to what this agent may actually call.

    Without this the gateway advertises the upstream's whole catalogue to every
    agent, so an agent discovers tools its registry record never granted,
    calls one, and gets a DENY it had no way to anticipate. The registry is the
    source of truth for capability (ADR-0008), so the listing an agent sees
    should be the registry's answer, not the upstream's.

    Applied after forwarding rather than in Rego on purpose: this is a
    projection of registry facts, and Rego holds only logic (ADR-0008). It also
    keeps the filter transport-agnostic — /mcp and /mcp/v1 cannot disagree
    about what an agent can see.

    A DENY never reaches here, and neither does an unregistered agent (the
    registry gate refuses those first), so `registration is None` only happens
    in a caller that skipped the gate; such a listing is filtered to empty
    rather than passed through whole.
    """
    if call.method != "tools/list" or "result" not in response:
        return response
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return response

    granted = set(call.registration.granted_tools) if call.registration else set()
    tools = [
        tool for tool in result["tools"] if isinstance(tool, dict) and tool.get("name") in granted
    ]
    return {**response, "result": {**result, "tools": tools}}


def _jsonrpc_error(
    request_id: str | int | None, *, code: int, message: str, instruction: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"instruction": instruction}},
    }
