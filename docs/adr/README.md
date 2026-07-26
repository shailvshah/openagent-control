# Architecture Decision Records

| ADR | Title |
|---|---|
| [0001](0001-hybrid-interception-pattern.md) | Hybrid interception pattern, gateway-first |
| [0002](0002-opa-rego-as-the-v1-policy-engine.md) | Open Policy Agent (Rego) as the v1 policy engine |
| [0003](0003-ed25519-hash-chained-audit-ledger.md) | Ed25519-signed, hash-chained audit receipts |
| [0004](0004-mcp-as-the-v1-protocol-surface.md) | MCP + OAuth 2.0 as the v1 governed protocol surface |
| [0005](0005-workload-identity-via-spiffe-stubbed-in-v1.md) | Workload identity via SPIFFE, stubbed in v1 |
| [0006](0006-hexagonal-architecture-for-the-control-plane.md) | Hexagonal (ports & adapters) architecture, incl. integration-support ports |
| [0007](0007-decentralized-identity-is-a-future-extension-not-v1-scope.md) | Decentralized identity (DIDs/VCs) is a future extension, not v1 scope |
| [0008](0008-agent-registry-as-source-of-truth.md) | Agent Registry as the source of truth for agent facts |
| [0009](0009-postgres-persistence-and-redis-caching.md) | Postgres persistence for the ledger and registry, Redis caching |
| [0010](0010-oidc-jwks-identity-for-okta-and-entra.md) | OIDC/JWKS identity validation for Okta and Microsoft Entra ID |
| [0011](0011-mcp-streamable-http-via-the-official-sdk.md) | Speak MCP via the official SDK, not hand-rolled JSON-RPC |
| [0012](0012-shadow-mode-for-first-deployment.md) | Shadow (observe) mode as a decision, not a bypass |
| [0013](0013-vault-transit-signing-key-custody.md) | KMS-backed receipt signing via HashiCorp Vault Transit |
| [0014](0014-control-plane-api-and-dashboard.md) | Control-plane API + dashboard, separate from the enforcing gateway |
| [0015](0015-real-mcp-ingress-transport.md) | Real MCP ingress transport, mirroring ADR-0011's outgoing fix |
| [0016](0016-multi-upstream-routing-and-listing-projection.md) | Many upstreams behind one gateway, and a listing that tells the truth |
| [0017](0017-client-sdk-and-authorize-only-endpoint.md) | A client SDK, and the authorize-only endpoint it needs |
| [0018](0018-dashboard-as-one-static-file.md) | The dashboard is one static file, not a SPA build |
| [0019](0019-sponsorship-is-approval-authorization-comes-from-the-user.md) | Sponsorship is approval; authorization comes from the user |
