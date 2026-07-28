# ADR-0020: Inbound agent-serving is out of scope; the boundary is `subject_token`

## Status
Accepted

## Context
Every framework example in this repo (`examples/langgraph_governed_agent/`,
`crewai_governed_agent/`, `google_adk_governed_agent/`, `strands_governed_agent/`)
demonstrates the same thing: an already-running agent process making *outbound*
tool calls, each identity-attested, policy-checked, and receipted by this
gateway. That is one half of a real deployment's authn/z surface. The other
half is never addressed by any of those examples, and conflating the two
would misdescribe what this project does.

**How agents are actually served in production, checked rather than assumed
(see the `crewai`/`google-adk`/`strands-agents`/`langgraph-orchestration`
skills' own research, and the framework docs each draws from):** an agent
object — a LangGraph compiled graph, a CrewAI `Crew`, an ADK `Agent` behind a
`Runner`, a Strands `Agent` — is constructed by its own orchestration
framework, then *served* behind one of a small number of transports: plain
HTTPS request/response, Server-Sent Events (streaming a turn's tokens back to
a caller — LangGraph Platform's and ADK's `Runner`'s default streaming shape),
WebSocket (bidirectional, for live/voice-style sessions), or the agent exposed
as an MCP server itself (JSON-RPC, the mirror image of what this gateway
already does for tools — ADR-0015). In every real deployment we found, **the
serving layer or the API gateway in front of it, not the agent framework
itself, terminates authn** — typically OIDC: the caller presents a token,
something validates it against a JWKS, and the *result* — a user identity,
their roles/claims — reaches the agent process as request context (a header,
a validated-claims object the framework's own middleware attaches, a session).

Call this **boundary 1**: who may invoke the agent at all. What this gateway
already does — identity-attest, policy-check, and receipt every *tool* call
the agent makes once it is already running — is **boundary 2**. They are not
the same question, and this project has only ever answered the second one.

`examples/oidc_identity_demo/` (removed by this ADR) demonstrated
`OidcJwksIdentityProvider` validating a *workload's* OIDC token — still
boundary 2's identity leg (the agent authenticating itself to the gateway),
not a boundary-1 example. Its removal isn't a scope cut; it was never boundary
1 in the first place, and keeping it around invited exactly the conflation
this ADR exists to resolve. `OidcJwksIdentityProvider` itself is unaffected —
still exercised by `tests/unit/test_oidc_jwks.py` and
`tests/integration/test_keycloak_conformance.py`.

## Decision

**Boundary 1 is explicitly out of scope for this project**, for the same
reason ADR-0007 put decentralized identity out of v1 scope: it is a large,
separately-solved problem — terminating HTTPS/SSE/WebSocket, session
management, rate limiting, an OIDC redirect/PKCE flow for human logins — that
established API gateways (Kong, Envoy, cloud-provider gateways) and the agent
frameworks' own serving layers (LangGraph Platform, ADK's Agent Engine,
Bedrock AgentCore for Strands) already do well. Building a second one here
would not differentiate this project; it would dilute it. This gateway's own
MCP-serving role (`POST /mcp`, ADR-0015) stands: it serves *tools*, not
*agents*, and that distinction doesn't change.

**What this project commits to instead is the contract at the seam.** A
boundary-1 gateway that has already validated a human's OIDC token has, by
definition, a verified user identity in hand. The integration point is
`subject_token=` on the agent's outbound calls (`GovernedClient`,
ADR-0019): whatever boundary 1 validated gets threaded through as the
subject's own token, verified independently at boundary 2
(`OAC_SUBJECT_VERIFICATION_MODE=oidc-jwks`) and exposed to Rego as
`input.subject.{id,roles,scopes}` — so policy can gate on the *user's*
entitlement, not just the agent's registry grant. This is not new work; it is
the existing ADR-0019 mechanism, named here as the explicit answer to "how do
the two boundaries compose."

Concretely, a real deployment looks like:

```
client → (boundary 1: OIDC login, HTTPS/SSE/WebSocket/MCP termination,
           whatever API gateway or framework serving layer does this)
       → agent process (LangGraph/CrewAI/ADK/Strands)
       → (boundary 2: this gateway) → tools/MCP servers
```

with the boundary-1-validated user's token passed as `subject_token=` across
the middle arrow.

### The actual wiring, not just the contract

"Out of scope" means this project doesn't terminate boundary 1 — it does not
mean a deployer is left to invent the handoff. Whatever boundary 1 already
does (an API gateway, an ASGI middleware, the framework's own auth hook)
converges on the same shape: by the time your agent's request handler runs,
the caller's bearer token is sitting in a header, already validated once at
the edge. The only wiring required is reading it and forwarding it —
one line, at the one place each framework hands you the inbound request:

| Serving layer | Where boundary 1's token shows up | What to do with it |
|---|---|---|
| FastAPI/Starlette in front of any framework (LangGraph, CrewAI, Strands) | `request.headers["authorization"]`, in your own route/middleware, *before* constructing the agent's tools | Pass its bearer value as `subject_token=` when building `GovernedClient` for that request |
| ADK `Runner` behind its own server | `google.adk.agents.readonly_context.ReadonlyContext` / `InvocationContext` exposes the inbound request state your server attached | Same — read it once, pass it into the tool factory for that invocation |
| An MCP client calling the agent as an MCP server | The `Authorization` header on the incoming MCP request (per the MCP authorization spec) | Same |

```python
# Boundary 1 already ran (API gateway / ASGI middleware validated the
# caller's OIDC token). This is the one line that bridges to boundary 2:
from openagent_control.sdk import GovernedClient

def build_oac_for_request(request) -> GovernedClient:
    user_token = request.headers["authorization"].removeprefix("Bearer ")
    return GovernedClient(
        "http://localhost:8000",
        token=agent_token,          # the agent's own workload identity (unchanged, per-process)
        subject_token=user_token,   # boundary 1's validated token, threaded through per-request
    )
```

Per-request, not per-process, is the point: `token=` (the agent's own
identity) is constructed once; `subject_token=` is re-derived from whichever
human's request triggered this particular tool call, so a shared agent
process serving many users still attributes and authorizes each call to the
right one. Verified server-side: `OidcSubjectVerifier` independently validates
whatever arrives as `subject_token` (signature, `iss`, `aud`, `exp` — it does
not trust that boundary 1 already checked it).

**Proven against a real stack, not just described**: [`examples/subject_bridge_demo/`](../../examples/subject_bridge_demo/README.md)
runs a real signed-JWT authorization server, a real `opa` process, a real MCP
server, and the real gateway. One stable, autonomous agent identity; two
different humans call it through the same `boundary1_app.py` endpoint (the
exact code this ADR documents); the real Rego policy allows the one with the
`finance-approver` role and denies the one without it — same agent, same
registry grant, only the attached `subject_token` differs.

Building that proof surfaced a real gap, not a documentation one:
`GovernedExecutionService` only ran subject verification when the *agent's
own* token carried a `human_sponsor` claim — which assumes the agent
re-authenticates per user (an OBO-style flow), not that a stable identity
forwards a different `subject_token` per request. That is exactly this
pattern's shape, and it had no test coverage at this level — the existing
`tests/integration/test_subject_authorization.py` calls `OPAPolicyEngine.evaluate()`
directly, bypassing this gate entirely. Fixed: subject verification now
triggers on the *subject token's presence*, not on the agent's own sponsor
claim (`_bind_subject` already treated an unset `human_sponsor` as "nothing
to bind against" rather than a mismatch, so the fix doesn't weaken binding).

## Consequences
- No new transport-handling code (HTTPS/SSE/WebSocket/MCP-server-for-agents)
  is added to this repo. Anyone asking "does openagent-control serve my
  agent" gets a direct no, pointed at this ADR and their framework's own
  serving docs.
- `examples/oidc_identity_demo/` is deleted; `docs/roadmap.md`'s reference to
  it is repointed at the unit/integration tests that actually cover
  `OidcJwksIdentityProvider`.
- The `subject_token`/`OAC_SUBJECT_VERIFICATION_MODE` mechanism (ADR-0019) is
  now documented as load-bearing for a second reason beyond delegated calls
  within a single agent process: it is the only supported bridge between
  whatever authenticates a human at boundary 1 and what this gateway can
  enforce at boundary 2.
- **Real fix, not just documentation**: `governed_execution.py`'s subject-
  verification trigger changed from `agent.human_sponsor` to the presence of
  a `subject_token` header — closing a gap that had zero test coverage before
  this ADR's proof example was built. A stable, autonomous agent identity
  serving many end-users can now have each request's subject independently
  verified and exposed to policy, which was silently impossible before.
- **Not addressed**: session-level concerns at boundary 1 (token refresh
  across a long streaming session, WebSocket reconnection carrying the same
  identity) are entirely the serving layer's problem; this gateway sees one
  tool call at a time and has no session concept to break.
