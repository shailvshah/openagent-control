# openagent-control
The zero-config security, governance, and telemetry proxy for the autonomous AI workforce.

## What it is

Autonomous agents typically run on static service accounts and hardcoded API keys.
That forces a choice between over-privileged agents and brittle workflows, and leaves
no cryptographically trustworthy record of what an agent actually did.

`openagent-control` sits between an agent and the tools it calls — today, MCP
(`tools/list` / `tools/call`) — and enforces three things on every call:

1. **Identity** — the agent is a workload with its own identity, not a shared secret.
2. **Policy** — every tool call is evaluated against explicit policy (Open Policy
   Agent / Rego) before it reaches the target system; a scoped, short-lived
   credential is issued only on allow.
3. **Audit** — every decision produces an Ed25519-signed, hash-chained receipt, so a
   compliance reviewer can prove after the fact what was authorized and executed.

See [docs/design.md](docs/design.md) for the full design and
[docs/adr/](docs/adr/README.md) for the reasoning behind each decision — including
what's a real implementation today versus a deliberate v1 stub.

## Status

Early foundation, not production-ready. In particular:

- **Identity is a stub.** The shipped `IdentityProvider` trusts an `X-Spiffe-ID`
  header rather than performing real SPIFFE/SPIRE attestation — see
  [ADR-0005](docs/adr/0005-workload-identity-via-spiffe-stubbed-in-v1.md). Only safe
  behind a network boundary that has already authenticated the caller.
- **Token exchange is a stub.** OAuth 2.0 On-Behalf-Of (RFC 8693) is modeled as a
  `TokenExchange` port but not wired to a real IdP (Okta, Microsoft Entra ID) yet —
  see [ADR-0004](docs/adr/0004-mcp-as-the-v1-protocol-surface.md).
- **The ledger's signing key is in-process** (regenerated on restart, not backed by a
  KMS/HSM) and chain state is single-process — see
  [ADR-0003](docs/adr/0003-ed25519-hash-chained-audit-ledger.md).
- No Human-in-the-Loop approval flow, no sidecar/native-SDK deployment pattern, no
  cross-organization (DID/VC) identity yet — tracked in
  [ADR-0001](docs/adr/0001-hybrid-interception-pattern.md) and
  [ADR-0007](docs/adr/0007-decentralized-identity-is-a-future-extension-not-v1-scope.md).

## Architecture

Hexagonal (ports & adapters) — see
[ADR-0006](docs/adr/0006-hexagonal-architecture-for-the-control-plane.md).

```
src/openagent_control/
├── domain/          # pure models + Protocol ports — no I/O, no framework imports
├── adapters/         # concrete implementations of each port (OPA, ledger, identity, ...)
└── gateway/           # FastAPI app: routes + dependency wiring, no policy/crypto logic
```

| Port | v1 adapter |
|---|---|
| `PolicyEngine` | Open Policy Agent (Rego), via `policies/mcp_authz.rego` |
| `IdentityProvider` | header-trusting stub |
| `Ledger` | Ed25519 hash-chained receipts |
| `TokenExchange` | stub RFC 8693 exchange |
| `MCPUpstream` | HTTP forward to a downstream MCP server |
| `AuditExporter` | stdout/log |

## Development

```bash
make install      # poetry install
make quality      # black --check, ruff, mypy
make test         # pytest with coverage (95% gate)
make check        # quality + test
make up           # docker compose: gateway + OPA + mock MCP server
```

Requires Python 3.11+ and [Poetry](https://python-poetry.org/).

Once running (`make up`), send a tool call through the gateway:

```bash
curl -X POST http://localhost:8000/mcp/v1 \
  -H "Content-Type: application/json" \
  -H "X-Spiffe-ID: spiffe://corp.net/ns/finance/agent/invoice-bot" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_query","arguments":{}}}'
```
