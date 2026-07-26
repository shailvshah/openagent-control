# ADR-0012: Shadow (observe) mode as a decision, not a bypass

## Status
Accepted

## Context
Every version of the rollout plan (see [roadmap.md](../roadmap.md)) sequences
Phase 2 as "observe first, enforce later" — a first production deployment
needs to see what a policy would block against real traffic before it
actually starts blocking anything. Without this, day one either means
enforcing an unvalidated policy against production (unacceptable blast
radius) or not deploying to real traffic at all (defeats the point of a first
deployment).

Naively, "observe mode" could mean: evaluate policy, log the decision, always
forward the call. That is simple but throws away exactly the property this
project is built around — ADR-0003's non-repudiable receipt chain. An
unenforced decision that isn't recorded and signed the same way an enforced
one is has no evidentiary value when the operator later asks "if we had
enforced this policy from day one, what would it have blocked?"

## Decision
`Settings.decision_mode: Literal["enforce", "observe"]`, consumed by
`GovernedExecutionService`. In `"observe"` mode, an explicit **policy** DENY
(the OPA-evaluated decision, not a registry-gate or fail-closed one) is:
- recorded and Ed25519-signed exactly as an enforced DENY would be, with a
  new `ExecutionReceipt.enforced = False` field carried through the chain,
- but the call is forwarded to the upstream anyway, as if it had been
  allowed.

Two categories of DENY are **never** shadowed, regardless of `decision_mode`:

1. **Registry-gate denials** (orphaned or suspended agents, ADR-0008). This is
   the zero-orphaned-agents guarantee, not a policy call — an operator running
   shadow mode to tune Rego should not discover that it silently disabled the
   agent registry's authorization boundary too.
2. **Fail-closed denials** (policy engine unreachable). An OPA outage is an
   infrastructure failure, not the kind of signal shadow mode exists to
   observe. Softening it would turn "the policy engine is down" into "every
   call is silently allowed," which is a worse failure mode than blocking.

Both keep `enforced=True` and behave identically to `decision_mode="enforce"`.

## Consequences
- `ExecutionReceipt.enforced` and `oac.execution_receipts.enforced`
  (migration `0002`) are new, backfilled `True` for existing rows — a receipt
  written before shadow mode existed was, by definition, enforced.
- The `Ledger.record()` port gained a keyword-only `enforced: bool = True`
  parameter. Both adapters (`Ed25519ChainLedger`, `PostgresLedger`) implement
  it identically, per ADR-0006's contract-testing expectation.
- An operator reviewing the audit chain after a rollout can answer "what
  would enforce mode have blocked" precisely: filter receipts where
  `decision = DENY and enforced = false`. That query is the deliverable —
  it's what turns shadow mode from a logging feature into evidence a policy
  is safe to flip to enforce.
- `decision_mode` is a deployment-wide setting, not per-agent or per-tool.
  Finer-grained rollout (e.g. shadow mode for one risk tier only) is not
  implemented; the registry's `risk_tier` field is available for a future
  policy-level version of this if needed, without a port change.
