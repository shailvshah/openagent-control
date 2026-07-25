# ADR-0001: Hybrid interception pattern, gateway-first

## Status
Accepted

## Context
Agents call tools via direct APIs or MCP. We need to sit between the agent and the
target system without requiring every target system, or every agent framework, to
change first. Three integration shapes are possible: a Kubernetes sidecar, an egress
gateway the agent's client points at, or a native SDK/middleware decorator.

## Decision
Support all three long-term, but build **Pattern B (egress MCP/LLM gateway)** first.
The agent's MCP or LLM client `BASE_URL` is pointed at the control plane; no changes
are required to the target system (Salesforce, Snowflake, DealCloud, etc.) and no
per-framework SDK work is required to get a first working system.

Sidecar injection (Pattern A) and the native SDK decorator (Pattern C) are follow-on
work once the gateway's domain logic (policy evaluation, credential brokering, audit)
is proven, since both patterns should reuse that same core rather than re-implement it.

## Consequences
- v1 has no story for HITL interruption mid-graph (that needs Pattern C's stateful
  hook into the agent framework) — tracked as a known gap, not solved in v1.
- The gateway must be transport-agnostic internally (JSON-RPC/MCP today, plain REST
  tomorrow) so Patterns A and C can call the same domain core instead of a duplicate.
- Native LLM function-calling interception — intercepting a provider's own tool-call
  protocol (e.g. OpenAI/Anthropic `tool_use`) rather than a call already routed through
  MCP — is out of scope for v1 and deferred to phase 2.
