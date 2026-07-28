# ADR-0016: Many upstreams behind one gateway, and a listing that tells the truth

## Status
Accepted

## Context
Two adoption defects, found by auditing the repo against the question "could a
team point an agent that is already running in production at this?"

**One gateway could front exactly one MCP server.** `Settings.mcp_upstream_url`
is a single string and `_mcp_upstream()` built a single adapter from it. Real
agents call several MCP servers — finance tools live with the finance data, CRM
tools with the CRM. The only way to govern that fleet was one gateway
deployment per upstream, and therefore one registry, one policy bundle, and one
audit chain per upstream, for what is logically one fleet of agents. Nothing in
the architecture required this; it was just never built.

**The gateway advertised tools it would refuse to run.** `tools/list` was
forwarded to the upstream and returned verbatim. An agent pointed at the
gateway therefore discovered the upstream's entire catalogue, including tools
its registry record never granted, called one, and got a DENY it had no way to
anticipate. For a drop-in deployment this is worse than a missing feature: the
agent's own tool list misleads it, and the resulting denials look like the
governance layer malfunctioning rather than working.

**A third defect surfaced while verifying the first two.** The shipped Rego
required an explicit `tool_check` rule per tool:

```rego
allow if { ... granted(input.params.name); tool_check(input.params.name, ...) }
tool_check("read_query", _) := true
```

An undefined `tool_check` makes the `allow` body fail, so any tool without a
hand-written rule was denied — and denied as *"Tool arguments exceed authorized
thresholds"*, with no arguments involved. Granting a tool in the registry was
silently not enough; you also had to edit policy. That directly contradicts
ADR-0008 ("the registry is the source of truth for what an agent may do") and
would have made every new tool, and every new upstream, a two-system change.
It was caught by the multi-upstream integration test below: a real CRM tool,
granted in the registry, denied by the real policy engine.

## Decision

### `RoutingMCPUpstream`
A new adapter implementing the existing `MCPUpstream` port over N named
upstreams, configured as `OAC_MCP_UPSTREAMS` (JSON, name → URL). `tools/list`
fans out concurrently and merges; the merge doubles as the tool → upstream
routing table, cached with a TTL and refreshed on a miss. `tools/call` looks up
the tool and forwards to the one upstream that advertised it. Because it
implements the same port, nothing upstream of it in the architecture changed —
no new port, no change to `GovernedExecutionService`.

Two behaviours chosen deliberately, both tested:
- **A partly-unreachable fleet still lists.** Only an all-upstreams-failed
  fan-out raises; otherwise the listing carries what the reachable servers
  advertised. A CRM outage should not blind an agent to the finance tools it
  could still use. An empty catalogue and a total outage mean very different
  things to an agent, so the total-outage case must not be reported as "no
  tools".
- **Tool-name collisions resolve to the first upstream in configured order**,
  and the loser is dropped from the merged listing rather than renamed.
  Renaming would hand the agent a name its upstream has never heard of.
  Configured order is therefore load-bearing.

`mcp_upstreams` takes precedence over `mcp_upstream_url` rather than merging
with it — folding the single-upstream default (`http://localhost:8080`) into a
configured fleet would add a phantom member to every multi-upstream deployment.
Unset, behaviour is exactly as before.

### The listing is projected down to the registry's grants
`GovernedExecutionService` filters a `tools/list` result to
`registration.granted_tools` before returning it. Placed in the application
service, not in Rego and not in the adapter, for three reasons: it is a
projection of registry facts and Rego holds only logic (ADR-0008); it must
apply identically to `/mcp` and `/mcp/v1`, which a transport-level filter could
not guarantee; and it must apply regardless of which upstream adapter is
configured. Only the `tools` array is rewritten — pagination cursors and any
other fields survive, or a paging client breaks silently.

### Guardrails narrow a grant; they do not constitute it
The Rego rewrite inverts `tool_check` into `guardrail_violation`: each rule now
states what is *forbidden* for one tool, so a tool with no rule is governed by
its registry grant alone, and the guardrails only ever narrow that grant.

## Consequences
- One gateway now governs a fleet of MCP servers: one registry, one policy
  bundle, one audit chain. Verified against **two real MCP servers**
  (`tests/integration/test_multi_upstream_routing.py`): a finance server and a
  new CRM server, each with real OAuth resource-server protection, behind one
  real gateway, driven by the real MCP SDK client — proving a merged listing
  and a routed call across both off one brokered credential. The single-URL
  setting is pointed at a dead address in that fixture, so a pass cannot come
  from a single-upstream fallback that happened to work.
- An agent's tool list is now the registry's answer, not the upstream's.
  Verified end-to-end: the real downstream advertises `update_record`, the
  agent is granted only `read_query`, and a real MCP client sees exactly
  `read_query`.
- **The Rego rules now have automated coverage at all**
  (`tests/integration/test_rego_policy.py`, against a real `opa` process).
  They previously had none — every policy test mocked OPA's HTTP response,
  which covered the adapter's parsing and nothing about the decisions. That
  gap is how the `tool_check` defect survived. The new tests assert both
  directions: a granted-but-unguarded tool is allowed, and an argument beyond
  a guardrail is still denied.
- Registry grants are now sufficient on their own, which is the point — but it
  means an operator who adds a tool to `granted_tools` has authorized it, with
  no second gate. That is the intended reading of ADR-0008, and it raises the
  stakes on registry review; it is not a change to make quietly. ADR-0021 adds
  per-grant terms (`approval_required`, `required_roles`) for the operator who
  wants a *narrower* gate than "granted means allowed" for one specific tool,
  without touching this rule.
- Not addressed here: per-upstream credentials. Every upstream in a fleet
  currently receives a credential brokered for the one `delegated_audience`,
  which suits a fleet sharing a resource audience and not a fleet where each
  server has its own. That is a `CredentialBroker` change, not a routing one.
