# ADR-0002: Open Policy Agent (Rego) as the v1 policy engine

## Status
Accepted

## Context
Every intercepted tool call needs a deterministic allow/deny decision, evaluated
against agent identity, requested tool, and arguments. Candidates: Open Policy Agent
(Rego), AWS Cedar, or a hand-rolled rules engine in Python.

## Decision
Use OPA/Rego for v1. It's a mature, widely-deployed, language-agnostic policy engine
with a stable HTTP API, which lets policy evaluation stay a separate process/service
from the gateway from day one — matching the ports-and-adapters structure in
[ADR-0006](0006-hexagonal-architecture-for-the-control-plane.md), where the policy
engine is a swappable adapter behind a `PolicyEngine` port.

Cedar is not rejected outright — it has stronger native support for ReBAC-style
relationship policies — but OPA has the larger ecosystem and lower operational
surprise for a v1 whose policies are still simple capability/argument checks.

## Consequences
- Policy evaluation is an HTTP call to a separate OPA process (or sidecar), adding
  network latency to every tool call. Acceptable for v1; revisit if it becomes a
  bottleneck (e.g. embed OPA via its Go/WASM SDK instead of a network hop).
- Policies are written in Rego, a non-mainstream language; policy authoring tooling
  (rule builder UI, simulation/replay) is future work, not v1.
- Switching to Cedar later means writing a new adapter behind the same
  `PolicyEngine` port — not a rewrite of the gateway.
