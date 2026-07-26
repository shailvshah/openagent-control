# Security Policy

`openagent-control` sits in the credential and authorization path for
autonomous agents. Treat any bypass of its identity, policy, or audit
guarantees as a security issue — including ones that only affect the examples
or CI, since those are what people copy into production.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security report.** Use
[GitHub Security Advisories](https://github.com/shailvshah/openagent-control/security/advisories/new)
("Report a vulnerability" on the Security tab) so the report and any fix stay
private until a patch is out. If that path is unavailable, email
shailvshah@gmail.com.

Please include:
- The affected version/commit and which adapter or mode (e.g.
  `identity_mode=oidc-jwks`, `token_exchange_mode=rfc8693`) is involved.
- Steps to reproduce, or a PoC — the closer to `tests/integration/` in shape,
  the faster it can be verified and turned into a regression test.
- What the bypass actually achieves (e.g. "an orphaned agent is not denied,"
  "the brokered credential is scoped to the wrong audience," "a denial is not
  receipted") — this project's threat model treats a **missing** or
  **unreceipted** denial as equally serious to a wrong allow.

You should get an acknowledgment within 5 business days. There is no bounty
program; credit in the advisory and release notes on request.

## Scope

**In scope:**
- The gateway's identity, policy-evaluation, credential-brokering, and
  audit-receipt paths (`src/openagent_control/`).
- The control plane's operator-identity, registry-CRUD, and receipt-search/
  verify paths (`src/openagent_control/control_plane/`, ADR-0014) — in
  particular, any path that would let it write `oac.execution_receipts` or
  produce a valid receipt signature. By design it should hold neither
  capability; a report showing otherwise is a critical finding.
- The packaging and release pipeline (a compromised release is a compromise of
  everyone who `pip install`s it).
- The example/demo stack (`examples/`), because it is the reference
  implementation most integrators will start from.

**Out of scope / known and tracked, not new reports:**
- The audit ledger's signing key defaults to in-process key generation.
  `OAC_SIGNING_KEY_MODE=vault-transit` (ADR-0013) keeps the key inside
  HashiCorp Vault instead — but that itself pushes trust onto how Vault is
  operated (HA, unsealing, backup), which is explicitly out of this project's
  scope. Don't file "the default mode isn't KMS-backed" as a new finding; it's
  documented, and the alternative is documented too. This also means the
  control plane's `verify_chain()` can't meaningfully verify signatures the
  gateway produced unless both share a Vault-backed key — see ADR-0014.
- `OAC_IDENTITY_MODE=header` trusts an `X-Spiffe-ID` header outright — it is
  documented as a dev-only stub (ADR-0005), safe only behind a boundary that
  has already authenticated the caller.
- `OAC_CONTROL_PLANE_OPERATOR_AUTH_MODE=api-key` is a static shared-secret
  bearer token, documented as the low-ceremony default (ADR-0014) — same
  posture as `identity_mode=header`. Use `oidc-jwks` for real operator access
  control.
- The `examples/enterprise_scenario/` authorization server ships a hardcoded
  client secret (`scenario-only-not-a-real-secret`) by design, for a
  fully-offline demo. It is gated behind the `demo` Docker Compose profile,
  never the default.

## Supported versions

Pre-1.0: only the latest published release on PyPI receives fixes. There is no
LTS branch yet.
