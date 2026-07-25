# Roadmap: 7-Month Enterprise Rollout vs. Current State

Status assessed 2026-07-25 against the v1 foundation (see [design.md](design.md),
[ADRs](adr/README.md)). "Seam exists" means the port/interface is declared per
ADR-0006 so the work is additive, not architectural.

## Where we are, phase by phase

| Phase | Target | Status | What exists today | What's missing |
|---|---|---|---|---|
| 1. Agent Registry & Identity Plane | M1-2 | 🟢 ~90% | **Master Agent Registry**, now a real system of record (ADR-0008 + ADR-0009): `PostgresAgentRegistry` (`oac.agents` table, own schema, timestamps incl. `status_changed_at`) is the production adapter; `FileAgentRegistry`/YAML remains the zero-dependency dev default and import source. Orphaned or suspended agents get a *receipted* DENY; Rego holds only logic. Registry reads are Redis-cached (30s TTL) so the hot path doesn't hit Postgres every call. **IdP adapters**: RFC 8693 (Okta-compatible) + Entra OBO token exchange, with exchanged tokens Redis-cached to their own `exp` (minus safety margin). **Identity validation, three real paths**: `JwtSvidIdentityProvider` (SPIFFE JWT-SVIDs) and, new, `OidcJwksIdentityProvider` (ADR-0010) — validates actual Okta/Entra-issued access tokens against their published JWKS (signature, issuer, audience, orphan-agent checks), built from a dedicated `enterprise-idp-integration` skill and verified against real signed tokens over a locally served JWKS — see `examples/oidc_identity_demo/` | Actual SPIRE server/agent deployment + Workload API (x509) attestation; an admin/kill-switch API to write registry status changes (Postgres makes this buildable, but nothing calls it yet — cache TTL is the only revocation-latency bound today); JWKS/trust-bundle rotation cadence tuning; tenant-independent Entra multi-tenant validation (signing-key-issuer-scope check, flagged as a gap in ADR-0010); live validation against a real Okta org or Entra tenant (only a mock IdP has been exercised) |
| 2. MCP Gateway in Shadow Mode | M3 | 🟢 ~65% | The full enforcing gateway: interception, OPA evaluation, fail-closed denials, semantic error payloads, Ed25519 hash-chained receipts, now durably persisted and replica-safe (`PostgresLedger`, row-locked chain head) — verified end-to-end with a real LangGraph agent (`examples/langgraph_governed_agent/`) | **Shadow/dry-run mode itself** (a `decision_mode: enforce\|observe` setting that logs would-be denials without blocking); OpenTelemetry spans (dependency declared, unused); Envoy sidecar variant (ADR-0001 Pattern A); policy-baselining tooling from observed traffic |
| 3. Ethical Walls & Read-Only Enforcement | M4 | 🟠 ~25% | Request-side ABAC: per-identity capability grants + argument thresholds in OPA; deny enforcement live | **Response-side filtering** (stripping data the sponsor isn't cleared for); `tools/list` filtering (claimed in ADR-0004, not implemented); read/write action classification in policy; iManage/NetDocuments/DealCloud target adapters |
| 4. Sandboxing & Business Diffs | M5-6 | 🟡 ~20% | `ApprovalChannel` port declared (no implementation); receipts are chained, signed, **and durable across restarts/replicas** (ADR-0009 closed the ADR-0003 gap on chain state) | MicroVM/sandbox execution for writes; Business Diff generation; Slack/Teams approval adapters; **sequence sealing** API (concept in ADR-0003, no endpoint); KMS/HSM-backed signing key — the signer is now caller-supplied (`ReceiptSigner`) but still generated in-process by default, not yet KMS-backed |
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
   token-exchange adapters, JWT-SVID validation, and now `OidcJwksIdentityProvider`
   (ADR-0010, validates real Okta/Entra access tokens via JWKS) all ship behind
   settings. Remaining: a SPIRE deployment itself (x509 Workload API attestation),
   trust-bundle/JWKS rotation cadence tuning, and tenant-independent Entra
   multi-tenant validation; the header mode remains dev-only per ADR-0005.
4. **OTel wiring** — Phase 2's whole purpose is telemetry; the dependency is in
   `pyproject.toml` but no spans are emitted.
5. ~~Signing key custody + shared chain state~~ — **shared chain state done**
   (ADR-0009): `PostgresLedger` persists receipts and serializes the chain head
   with a row lock, correct across replicas and restarts. **Key custody remains
   open**: `ReceiptSigner` accepts an injected key, but nothing yet sources one
   from a KMS/HSM — still the blocker for receipts as compliance evidence.
6. **Admin/kill-switch API** — newly enabled, not yet built: registry status is a
   database row now (was a git file), so an operator surface that flips it is a
   small addition, not an architecture change. Until it exists, suspending an
   agent still means editing `oac.agents` directly, with revocation visible to the
   gateway within one cache TTL (default 30s).

## Honest framing

What we have is the **control-plane software** the plan presumes: hexagonal core,
policy engine, audit ledger, working agent integration, 100% test coverage. What we
do not have is any of the plan's **operational infrastructure** (SPIRE, IdP
federation, registry, SIEM, sandboxes) or its **enterprise integrations** (iManage,
Slack approvals, billing). The plan's calendar hasn't started; the foundation it
needs on day one is done.
