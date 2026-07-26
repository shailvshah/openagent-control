# Roadmap: 7-Month Enterprise Rollout vs. Current State

Status assessed 2026-07-26 against the v1 foundation (see [design.md](design.md),
[ADRs](adr/README.md)). "Seam exists" means the port/interface is declared per
ADR-0006 so the work is additive, not architectural.

## Distribution (new since the last assessment, not part of the 5-phase plan)

`pip install openagent-control` and `docker compose up` are both real and
conformance-tested (`tests/integration/test_packaging.py` builds a wheel,
installs it into a clean venv, and runs it from an unrelated working
directory). `openagent-control doctor` / `GET /readyz` share one
implementation so neither can bless a deployment the other refuses. CI
enforces lint, strict types, a 95% coverage gate, packaging conformance and
the end-to-end scenario against real Postgres/Redis/OPA on every push; release
publishes to PyPI via Trusted Publishing on a tag. See
[deployment.md](deployment.md) and [releasing.md](releasing.md).

## Scope decisions from the external-readiness audit (2026-07-26)

- **SAML is out of scope, not a gap.** The identity design (ADR-0005, ADR-0007,
  ADR-0010) is SPIFFE + OIDC/OAuth2. Every enterprise IdP that matters here
  (Okta, Entra ID, Keycloak, Auth0, PingFederate) speaks OIDC, and
  `OidcJwksIdentityProvider` already covers it — conformance-tested against a
  real Keycloak realm. Building a SAML assertion adapter would add real attack
  surface (XML signature validation is a notorious source of auth bypasses)
  for a protocol the industry is moving away from, with no identified IdP that
  actually requires it. Revisit only if a specific integration needs it.
- **A self-hosted control-plane API + read-only dashboard is in scope**,
  separate from the enforcing gateway: registry CRUD, receipt search/verify,
  fleet health. Same self-hosted posture as the gateway (ADR discussion,
  2026-07-25) — the vendor never holds the token-exchange secret or sits in
  the customer's data path. **The API is done (ADR-0014); the dashboard SPA
  is not started yet.** Tracked below.

## Scope decisions from the integrations discussion (2026-07-25)

Four integration axes were named as in scope, none started yet:

- **Agent-framework SDK plugins** — a decorator/plugin surface embedded in an
  agent framework's own call sites, routing those calls through this
  project's identity, policy, and audit path. This is a new SDK package, not
  an extension of the gateway — its own distribution, versioning, and
  framework-conformance testing (same "verify against the real framework"
  discipline as everything else here).
- **Enterprise target-system adapters** — Phase 3's response-side filtering,
  already on the roadmap above. Needs a real sandbox/trial account per system
  to verify against before it can ship.
- **Approval-channel adapters** — Phase 4's `ApprovalChannel` port, already on
  the roadmap above. Needs a real chat-platform workspace/tenant to verify
  against.
- **Control-plane API + dashboard docs** — document the API/dashboard above
  once built.

**Sequencing (2026-07-25): control-plane API + dashboard next**, once OTel
spans land — no external sandbox required (just this project's own Postgres),
and it makes the other three easier to demo (e.g. an approval-channel adapter
has somewhere to surface into). The SDK-plugin work and the two
sandbox-dependent adapters remain explicitly queued behind it, not started.

## Where we are, phase by phase

| Phase | Target | Status | What exists today | What's missing |
|---|---|---|---|---|
| 1. Agent Registry & Identity Plane | M1-2 | 🟢 ~90% | **Master Agent Registry**, now a real system of record (ADR-0008 + ADR-0009): `PostgresAgentRegistry` (`oac.agents` table, own schema, timestamps incl. `status_changed_at`) is the production adapter; `FileAgentRegistry`/YAML remains the zero-dependency dev default and import source. Orphaned or suspended agents get a *receipted* DENY; Rego holds only logic. Registry reads are Redis-cached (30s TTL) so the hot path doesn't hit Postgres every call. **IdP adapters**: RFC 8693 (Okta-compatible) + Entra OBO token exchange, with exchanged tokens Redis-cached to their own `exp` (minus safety margin). **Identity validation, three real paths**: `JwtSvidIdentityProvider` (SPIFFE JWT-SVIDs) and, new, `OidcJwksIdentityProvider` (ADR-0010) — validates actual Okta/Entra-issued access tokens against their published JWKS (signature, issuer, audience, orphan-agent checks), built from a dedicated `enterprise-idp-integration` skill and verified against real signed tokens over a locally served JWKS — see `examples/oidc_identity_demo/` | Actual SPIRE server/agent deployment + Workload API (x509) attestation; an admin/kill-switch API to write registry status changes (Postgres makes this buildable, but nothing calls it yet — cache TTL is the only revocation-latency bound today); JWKS/trust-bundle rotation cadence tuning; tenant-independent Entra multi-tenant validation (signing-key-issuer-scope check, flagged as a gap in ADR-0010); live validation against a real Okta org or Entra tenant specifically — though the identity and RFC 8693 adapters are now conformance-tested against a real third-party IdP (Keycloak 26.4, `tests/integration/test_keycloak_conformance.py`), which caught a genuine app-only-token misclassification bug |
| 2. MCP Gateway in Shadow Mode | M3 | 🟢 ~80% | The full enforcing gateway: interception, OPA evaluation, fail-closed denials, semantic error payloads, Ed25519 hash-chained receipts, durably persisted and replica-safe (`PostgresLedger`, row-locked chain head). Now verified against a **fully real stack** (`examples/enterprise_scenario/` + `tests/integration/`): real OIDC identity, real `opa` process, real RFC 8693 exchange, and a real MCP server that validates the brokered credential's audience and scope — which is what makes the **gateway-bypass refusal** demonstrable rather than asserted. Credential brokering now covers autonomous agents too (previously a placeholder string no real upstream would accept). **The gateway now speaks real MCP transport in both directions.** Outgoing (ADR-0011): the previous upstream adapter POSTed bare JSON-RPC, which any genuine MCP server rejects with 406; verified against GitHub's production MCP server. Incoming (ADR-0015, closing the mirror-image gap ADR-0011 left on this side): `POST /mcp` now exposes the gateway itself as a real MCP server (handshake, session, SSE) via the same SDK, so a genuine MCP client — not just an internal caller that already speaks bare JSON-RPC to `/mcp/v1` — can connect directly. Verified against the **real MCP SDK client** driving the full real stack (real OPA, real auth server, real downstream MCP server, real gateway process). **OpenTelemetry spans now real**: `GovernedExecutionService` emits a root span plus `identify`/`registry.lookup`/`policy_evaluate`/`broker_credential`/`forward` child spans, exported via OTLP/HTTP when `OAC_OTEL_ENABLED=true` and verified against a real local `otelcol` binary receiving and parsing the actual wire protocol, not the SDK's in-memory exporter | Envoy sidecar variant (ADR-0001 Pattern A); policy-baselining tooling from observed traffic; **MCP session pooling** — one session per tool call costs an extra initialize round trip, unmeasured under load (ADR-0011) |
| 3. Ethical Walls & Read-Only Enforcement | M4 | 🟠 ~25% | Request-side ABAC: per-identity capability grants + argument thresholds in OPA; deny enforcement live | **Response-side filtering** (stripping data the sponsor isn't cleared for); `tools/list` filtering (claimed in ADR-0004, not implemented); read/write action classification in policy; iManage/NetDocuments/ target adapters |
| 4. Sandboxing & Business Diffs | M5-6 | 🟡 ~25% | `ApprovalChannel` port declared (no implementation); receipts are chained, signed, **and durable across restarts/replicas** (ADR-0009); **KMS-backed signing key now real** (ADR-0013): `signing_key_mode=vault-transit` never lets the Ed25519 key leave HashiCorp Vault, verified against a real Vault dev server | MicroVM/sandbox execution for writes; Business Diff generation; Slack/Teams approval adapters; **sequence sealing** API (concept in ADR-0003, no endpoint); production Vault operations (HA, unsealing, backup) are explicitly out of this project's scope per ADR-0013 — it treats Vault as an operated external dependency, same as Postgres |
| 5. Chargebacks & Compliance Reporting | M7+ | 🔴 0% | Receipt schema carries what a billing/compliance export would need | Everything: Elite 3E integration, value telemetry, SOC 2 / EU AI Act export generation |

## The critical path to starting Month 1 for real

In dependency order — each unblocks the phase next to it:

1. **External-sharing hygiene** (in progress, 2026-07-26): `SECURITY.md` and
   `CONTRIBUTING.md` — publishing something that asks to be trusted with
   credentials and audit evidence without a vulnerability-disclosure path is a
   real gap, not cosmetic.
2. **Shadow mode toggle** (small: a `Settings.decision_mode` consumed by
   `GovernedExecutionService`; DENY decisions are receipted but the call forwards).
   Without it, Phase 2's "observe first, enforce later" sequencing is impossible and
   day-one deployment blocks production traffic.
3. ~~Agent Registry as data, not code~~ — **done** (ADR-0008): `registry/agents.yaml`
   is the source of truth; Rego holds only logic; orphans are receipted DENYs.
4. ~~Real identity adapters~~ — **largely done**: Okta (RFC 8693) and Entra (OBO)
   token-exchange adapters, JWT-SVID validation, and now `OidcJwksIdentityProvider`
   (ADR-0010, validates real Okta/Entra access tokens via JWKS) all ship behind
   settings. Remaining: a SPIRE deployment itself (x509 Workload API attestation),
   trust-bundle/JWKS rotation cadence tuning, and tenant-independent Entra
   multi-tenant validation; the header mode remains dev-only per ADR-0005.
5. ~~OTel wiring~~ — **done**: `GovernedExecutionService` emits spans
   (`governed_execution.execute` root + `identify`/`registry.lookup`/
   `policy_evaluate`/`broker_credential`/`forward` children) via
   `opentelemetry.trace.get_tracer(...)`, which is always a real tracer — a
   no-op one until `OAC_OTEL_ENABLED=true` configures a TracerProvider, so
   instrumentation itself has zero cost or risk when tracing is off. Verified
   against a real local OTel Collector binary (`otelcol`), not the SDK's
   in-memory exporter — the collector actually receives and parses the
   OTLP/HTTP payload. Not tied to any vendor backend.
6. ~~Signing key custody~~ — **done** (ADR-0013): shared chain state (ADR-0009)
   plus a `Signer` port with a real HashiCorp Vault Transit adapter
   (`signing_key_mode=vault-transit`) — the private Ed25519 key never leaves
   Vault, verified against a real local Vault dev server (sign, then
   independently verify with `cryptography` against Vault's own returned
   public key, plus a full `Ed25519ChainLedger` sign-and-chain cycle).
   `in-process` (regenerated on restart) remains the default, same posture as
   `identity_mode=header` — a documented dev stub, not silently good enough.
   AWS KMS and Azure Key Vault were considered and ruled out: neither supports
   Ed25519 asymmetric signing, so either would force reopening ADR-0003's
   algorithm choice rather than just relocating the key.
7. **Control-plane API** — **done** for the JSON API (ADR-0014); the
   dashboard SPA is the remaining piece. `openagent-control serve-control-plane`
   runs a separate, self-hosted service — registry CRUD, receipt search/verify,
   fleet health — backed by the same Postgres, sharing nothing else with the
   enforcing gateway (it never imports `GovernedExecutionService`, the policy
   engine, or the MCP upstream client). Operator identity, not workload
   identity: `OAC_CONTROL_PLANE_OPERATOR_AUTH_MODE=api-key` (a static token) or
   `oidc-jwks` (a real operator's OIDC access token checked against a
   role/group claim, covering Okta/Entra/Keycloak's differing claim shapes).
   Holds only the receipt-signing *public* key, never anything capable of
   `.sign()`, and never writes `oac.execution_receipts` — a compromise there
   has no path to forging receipts or bypassing policy. Every mutating call
   writes an `oac.operator_actions` audit row in the same transaction.
   Subsumes the "admin/kill-switch API" item from the previous assessment:
   registry status is a database row now (was a git file), so an operator
   surface that flips it was additive, not an architecture change. Verified
   against a **fully real stack**
   (`tests/integration/test_control_plane_e2e.py`): create an agent via the
   control-plane API, confirm a real gateway (real OPA, real Postgres) now
   allows it, suspend it via the same API, confirm the real gateway now denies
   it — closing the exact gap ADR-0009 flagged as out of scope. One caveat
   found during that verification: `verify_chain()` only produces meaningful
   cross-process signature checks when both services run
   `signing_key_mode=vault-transit`; the default `in-process` mode gives the
   gateway and the control plane independent random keys, so the control plane
   can search/list receipts either way but can't verify signatures the
   gateway's process produced without a shared key. Not yet built: the
   dashboard SPA itself (a Vite/React app served as static assets from the
   same service) and its browser-appropriate OIDC login (session cookie, on
   top of the same operator-identity port).

## Honest framing

What we have is the **control-plane software** the plan presumes: hexagonal core,
policy engine, audit ledger, working agent integration, 100% test coverage. What we
do not have is any of the plan's **operational infrastructure** (SPIRE, IdP
federation, SIEM, sandboxes) or its **enterprise integrations** (iManage,
Slack approvals, billing). The plan's calendar hasn't started; the foundation it
needs on day one is done.

The end-to-end path is now proven against real components rather than fakes —
see `examples/enterprise_scenario/`. Its stated limits are the honest ones: the
IdP and the downstream API run on localhost rather than in a tenant, and the
invoice data is fixtures. The receipt signing key defaults to in-process, but
a real alternative now exists (ADR-0013, `signing_key_mode=vault-transit`) and
isn't just declared — it's verified against a real Vault instance. Nothing in
the protocol, crypto, or policy path is simulated.
