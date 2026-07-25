# Roadmap: 7-Month Enterprise Rollout vs. Current State

Status assessed 2026-07-25 against the v1 foundation (see [design.md](design.md),
[ADRs](adr/README.md)). "Seam exists" means the port/interface is declared per
ADR-0006 so the work is additive, not architectural.

## Where we are, phase by phase

| Phase | Target | Status | What exists today | What's missing |
|---|---|---|---|---|
| 1. Agent Registry & Identity Plane | M1-2 | 🟢 ~75% | **Master Agent Registry** (`registry/agents.yaml` + `AgentRegistry` port, ADR-0008): agents cataloged with purpose/owner/risk-tier/status/grants; orphaned or suspended agents get a *receipted* DENY; Rego is now generic logic over registry facts. **IdP adapters**: RFC 8693 (Okta-compatible) + Entra OBO token exchange, settings-selected. **Cryptographic identity**: `JwtSvidIdentityProvider` validates SPIFFE JWT-SVIDs (signature/audience/expiry) — the shape SPIRE issues | Actual SPIRE server/agent deployment + Workload API (x509) attestation; registry lifecycle tooling (approval workflow, expiry); JWKS/trust-bundle rotation (v1 uses a static PEM); live validation against a real Okta/Entra tenant |
| 2. MCP Gateway in Shadow Mode | M3 | 🟢 ~65% | The full enforcing gateway: interception, OPA evaluation, fail-closed denials, semantic error payloads, Ed25519 hash-chained receipts — verified end-to-end with a real LangGraph agent (`examples/langgraph_governed_agent/`) | **Shadow/dry-run mode itself** (a `decision_mode: enforce\|observe` setting that logs would-be denials without blocking); OpenTelemetry spans (dependency declared, unused); Envoy sidecar variant (ADR-0001 Pattern A); policy-baselining tooling from observed traffic |
| 3. Ethical Walls & Read-Only Enforcement | M4 | 🟠 ~25% | Request-side ABAC: per-identity capability grants + argument thresholds in OPA; deny enforcement live | **Response-side filtering** (stripping data the sponsor isn't cleared for); `tools/list` filtering (claimed in ADR-0004, not implemented); read/write action classification in policy; iManage/NetDocuments/DealCloud target adapters |
| 4. Sandboxing & Business Diffs | M5-6 | 🔴 ~10% | `ApprovalChannel` port declared (no implementation); receipts are chained and signed | MicroVM/sandbox execution for writes; Business Diff generation; Slack/Teams approval adapters; **sequence sealing** API (concept in ADR-0003, no endpoint); KMS/HSM-backed signing key and multi-replica chain state — both explicitly deferred in ADR-0003 and blocking for production evidence |
| 5. Chargebacks & Compliance Reporting | M7+ | 🔴 0% | Receipt schema carries what a billing/compliance export would need | Everything: Intapp Time / Elite 3E integration, value telemetry, SOC 2 / EU AI Act export generation |

## The critical path to starting Month 1 for real

In dependency order — each unblocks the phase next to it:

1. **Shadow mode toggle** (small: a `Settings.decision_mode` consumed by
   `GovernedExecutionService`; DENY decisions are receipted but the call forwards).
   Without it, Phase 2's "observe first, enforce later" sequencing is impossible and
   day-one deployment blocks production traffic.
2. ~~Agent Registry as data, not code~~ — **done** (ADR-0008): `registry/agents.yaml`
   is the source of truth; Rego holds only logic; orphans are receipted DENYs.
3. ~~Real identity adapters~~ — **largely done**: Okta (RFC 8693) and Entra (OBO)
   token-exchange adapters plus JWT-SVID validation ship behind settings. Remaining:
   a SPIRE deployment itself (x509 Workload API attestation) and trust-bundle/JWKS
   rotation; the header mode remains dev-only per ADR-0005.
4. **OTel wiring** — Phase 2's whole purpose is telemetry; the dependency is in
   `pyproject.toml` but no spans are emitted.
5. **Signing key custody + shared chain state** — prerequisite for any receipt being
   used as compliance evidence (Phases 4-5), tracked in ADR-0003.

## Honest framing

What we have is the **control-plane software** the plan presumes: hexagonal core,
policy engine, audit ledger, working agent integration, 100% test coverage. What we
do not have is any of the plan's **operational infrastructure** (SPIRE, IdP
federation, registry, SIEM, sandboxes) or its **enterprise integrations** (iManage,
Slack approvals, billing). The plan's calendar hasn't started; the foundation it
needs on day one is done.
