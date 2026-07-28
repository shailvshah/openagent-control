# Roadmap: 7-Month Enterprise Rollout vs. Current State

Status assessed 2026-07-26 against the v1 foundation (see [design.md](design.md),
[ADRs](adr/README.md)). "Seam exists" means the port/interface is declared per
ADR-0006 so the work is additive, not architectural.

## Ship it today: what's real, concretely

- **Deploy:** `pip install openagent-control` or `docker compose up`, both
  conformance-tested (`tests/integration/test_packaging.py` builds a wheel,
  installs it clean, runs it from an unrelated directory). `openagent-control
  doctor` / `GET /readyz` share one implementation, so neither can bless a
  deployment the other refuses. Release publishes to PyPI via Trusted
  Publishing on a tag. See [deployment.md](deployment.md).
- **Identity:** `OAC_IDENTITY_MODE=oidc-jwks` validates real Okta/Entra
  ID/Keycloak access tokens (discovery + JWKS, `iss`/`aud`/`exp`) — no SAML,
  by deliberate choice (every enterprise IdP that matters here already speaks
  OIDC; a SAML adapter would add real attack surface for a protocol nothing
  here needs). Conformance-tested against a real Keycloak realm, not just the
  providers' published shapes on paper ([ADR-0010](adr/0010-oidc-jwks-identity-for-okta-and-entra.md)).
- **Frameworks:** `@governed`, `GovernedClient`, and `sdk.langchain`
  (`govern()`, `proxied_tools()`) ship in the main wheel — conformance-tested
  against real LangChain and a real compiled LangGraph graph, which is what
  caught proxied tools shipping no argument schema
  ([ADR-0017](adr/0017-client-sdk-and-authorize-only-endpoint.md)).
- **Fleet operations:** a control-plane API + dashboard, separate process from
  the enforcing gateway, registry CRUD + receipt search/verify + fleet health
  ([ADR-0014](adr/0014-control-plane-api-and-dashboard.md),
  [ADR-0018](adr/0018-dashboard-as-one-static-file.md)). Remaining: browser
  OIDC redirect login (today it's a pasted operator credential).
- **Multiple upstreams, one gateway:** `RoutingMCPUpstream` merges `tools/list`
  across N MCP servers and routes each call to whichever advertised it
  ([ADR-0016](adr/0016-multi-upstream-routing-and-listing-projection.md)),
  and a listing is filtered to exactly what the calling agent's registry grant
  allows — it never discovers a tool a call would then be denied for.

The two integration axes still not started — enterprise target-system
adapters (response-side filtering) and chat-platform approval adapters — are
blocked on the same thing: a real sandbox or tenant to verify against, not
design work. This project does not ship an adapter it hasn't run against the
real system.

## Where we are, phase by phase

| Phase | Target | Status | What exists today | What's missing |
|---|---|---|---|---|
| 1. Agent Registry & Identity Plane | M1-2 | 🟢 ~90% | **Master Agent Registry**, now a real system of record (ADR-0008 + ADR-0009): `PostgresAgentRegistry` (`oac.agents` table, own schema, timestamps incl. `status_changed_at`) is the production adapter; `FileAgentRegistry`/YAML remains the zero-dependency dev default and import source. Orphaned or suspended agents get a *receipted* DENY; Rego holds only logic. Registry reads are Redis-cached (30s TTL) so the hot path doesn't hit Postgres every call. **IdP adapters**: RFC 8693 (Okta-compatible) + Entra OBO token exchange, with exchanged tokens Redis-cached to their own `exp` (minus safety margin). **Identity validation, three real paths**: `JwtSvidIdentityProvider` (SPIFFE JWT-SVIDs) and, new, `OidcJwksIdentityProvider` (ADR-0010) — validates actual Okta/Entra-issued access tokens against their published JWKS (signature, issuer, audience, orphan-agent checks), built from a dedicated `enterprise-idp-integration` skill and verified against real signed tokens over a locally served JWKS (`tests/unit/test_oidc_jwks.py`) and a real Keycloak realm (`tests/integration/test_keycloak_conformance.py`) | Actual SPIRE server/agent deployment + Workload API (x509) attestation; an admin/kill-switch API to write registry status changes (Postgres makes this buildable, but nothing calls it yet — cache TTL is the only revocation-latency bound today); JWKS/trust-bundle rotation cadence tuning; tenant-independent Entra multi-tenant validation (signing-key-issuer-scope check, flagged as a gap in ADR-0010); live validation against a real Okta org or Entra tenant specifically — though the identity and RFC 8693 adapters are now conformance-tested against a real third-party IdP (Keycloak 26.4, `tests/integration/test_keycloak_conformance.py`), which caught a genuine app-only-token misclassification bug |
| 2. MCP Gateway in Shadow Mode | M3 | 🟢 ~80% | The full enforcing gateway: interception, OPA evaluation, fail-closed denials, semantic error payloads, Ed25519 hash-chained receipts, durably persisted and replica-safe (`PostgresLedger`, row-locked chain head). Now verified against a **fully real stack** (`examples/enterprise_scenario/` + `tests/integration/`): real OIDC identity, real `opa` process, real RFC 8693 exchange, and a real MCP server that validates the brokered credential's audience and scope — which is what makes the **gateway-bypass refusal** demonstrable rather than asserted. Credential brokering now covers autonomous agents too (previously a placeholder string no real upstream would accept). **The gateway now speaks real MCP transport in both directions.** Outgoing (ADR-0011): the previous upstream adapter POSTed bare JSON-RPC, which any genuine MCP server rejects with 406; verified against GitHub's production MCP server. Incoming (ADR-0015, closing the mirror-image gap ADR-0011 left on this side): `POST /mcp` now exposes the gateway itself as a real MCP server (handshake, session, SSE) via the same SDK, so a genuine MCP client — not just an internal caller that already speaks bare JSON-RPC to `/mcp/v1` — can connect directly. Verified against the **real MCP SDK client** driving the full real stack (real OPA, real auth server, real downstream MCP server, real gateway process). **OpenTelemetry spans now real**: `GovernedExecutionService` emits a root span plus `identify`/`registry.lookup`/`policy_evaluate`/`broker_credential`/`forward` child spans, exported via OTLP/HTTP when `OAC_OTEL_ENABLED=true` and verified against a real local `otelcol` binary receiving and parsing the actual wire protocol, not the SDK's in-memory exporter | Envoy sidecar variant (ADR-0001 Pattern A); policy-baselining tooling from observed traffic; **MCP session pooling** — one session per tool call costs an extra initialize round trip, unmeasured under load (ADR-0011) |
| 3. Ethical Walls & Read-Only Enforcement | M4 | 🟠 ~25% | Request-side ABAC: per-identity capability grants + argument thresholds in OPA; deny enforcement live | **Response-side filtering** (stripping data the sponsor isn't cleared for) — the remaining half; ~~`tools/list` filtering~~ **done** (ADR-0016: a listing is projected down to the agent's registry grants, so discovery never promises what policy will deny); read/write action classification in policy; iManage/NetDocuments/ target adapters |
| 4. Sandboxing & Business Diffs | M5-6 | 🟡 ~25% | `ApprovalChannel` port declared (no implementation); receipts are chained, signed, **and durable across restarts/replicas** (ADR-0009); **KMS-backed signing key now real** (ADR-0013): `signing_key_mode=vault-transit` never lets the Ed25519 key leave HashiCorp Vault, verified against a real Vault dev server | MicroVM/sandbox execution for writes; Business Diff generation; Slack/Teams approval adapters; **sequence sealing** API (concept in ADR-0003, no endpoint); production Vault operations (HA, unsealing, backup) are explicitly out of this project's scope per ADR-0013 — it treats Vault as an operated external dependency, same as Postgres |
| 5. Chargebacks & Compliance Reporting | M7+ | 🔴 0% | Receipt schema carries what a billing/compliance export would need | Everything: Elite 3E integration, value telemetry, SOC 2 / EU AI Act export generation |

## Foundational work, all done

Everything that used to gate Month 1 is now shipped:

- `SECURITY.md` / `CONTRIBUTING.md` — a vulnerability-disclosure path exists.
- Shadow mode (`OAC_DECISION_MODE=observe`) — denials are receipted but not
  enforced, for a safe first rollout ([ADR-0012](adr/0012-shadow-mode-for-first-deployment.md)).
- Agent registry as data (ADR-0008) — file or Postgres, never Rego.
- Real identity adapters — OIDC/JWKS (Okta/Entra/Keycloak), JWT-SVID, RFC 8693
  and Entra OBO token exchange, all conformance-tested against a real
  Keycloak realm ([ADR-0010](adr/0010-oidc-jwks-identity-for-okta-and-entra.md)).
  Remaining: an actual SPIRE deployment, and tenant-independent Entra
  multi-tenant validation.
- OTel tracing — real spans, OTLP/HTTP, verified against a real `otelcol`
  binary; zero cost when `OAC_OTEL_ENABLED` is off.
- Vault Transit signing-key custody ([ADR-0013](adr/0013-vault-transit-signing-key-custody.md))
  — the Ed25519 private key never leaves Vault, verified against a real Vault
  dev server. `in-process` (regenerated on restart) remains the dev default.
- Control-plane API + dashboard ([ADR-0014](adr/0014-control-plane-api-and-dashboard.md),
  [ADR-0018](adr/0018-dashboard-as-one-static-file.md)) — a separate,
  self-hosted service (never imports the policy engine or MCP client),
  operator identity (static key or OIDC role-claim check), every mutation
  audited in `oac.operator_actions`. One caveat: cross-process signature
  verification only works when both the gateway and control plane run
  `signing_key_mode=vault-transit` — under the default `in-process` mode each
  has its own random key. Remaining: browser OIDC redirect login (today it's
  a pasted operator credential).

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
