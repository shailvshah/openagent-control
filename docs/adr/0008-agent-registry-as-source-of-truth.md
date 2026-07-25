# ADR-0008: Agent Registry as the source of truth for agent facts

## Status
Accepted

## Context
Until now the only record of which agents exist and what they may do was the
`granted_tools` map hardcoded inside `policies/mcp_authz.rego`. That conflates two
different things — *facts about agents* (who they are, who owns them, what they're
for, whether they're active, which tools they've been granted) and *authorization
logic* (under what conditions a granted tool call is allowed). It also means
registering or suspending an agent is a policy-code change, and an agent whose
SPIFFE ID appears in no map is simply "denied by default" with no signal that it's
an orphan.

The rollout plan requires a Master Agent Registry with zero orphaned agents: every
agent cataloged with its purpose, owner, and risk tier before it touches anything.

## Decision
Introduce an `AgentRegistry` port and a `RegisteredAgent` domain model
(spiffe_id, display name, purpose, owner, risk tier, status, granted tools).

- The **registry is the source of truth for agent facts**. v1 ships a
  `FileAgentRegistry` reading `registry/agents.yaml` (config-as-code, reviewed via
  git PRs — the right first step for an enterprise; a database- or IdP-group-backed
  adapter is a later drop-in behind the same port).
- The **gateway enforces registration before policy**: an unregistered or suspended
  SPIFFE ID produces a *receipted DENY* (reason "agent not registered" / "agent
  suspended"), not a bare auth error — orphan attempts must appear in the audit
  ledger, not vanish.
- **OPA evaluates logic against registry facts, not embedded data**: the gateway
  attaches the `RegisteredAgent` to the policy input, and the Rego rules become
  generic ("allow if the requested tool is in `input.agent.granted_tools` and
  arguments pass `tool_check`"). Argument-level thresholds stay in Rego — they are
  authorization logic, not agent facts.

## Consequences
- Registering, suspending, or re-scoping an agent is now a data change
  (`registry/agents.yaml`), not a Rego change; policy code stops growing per agent.
- The registry lookup adds one async call per request; `FileAgentRegistry` caches
  the parsed file in memory, so this is negligible until a remote registry adapter
  exists (which will need its own caching policy).
- The alternative — pushing registry data into OPA as data documents/bundles — was
  rejected for v1: it adds a sync pipeline between two sources of truth. If policy
  evaluation ever moves fully server-side (Cedar/OPA bundles at scale), revisit.
- Per-request human sponsor (who the agent acts for right now) remains distinct
  from the registry's `owner` (who is accountable for the agent's existence).
