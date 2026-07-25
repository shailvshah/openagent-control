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

See [docs/design.md](docs/design.md) for the full design,
[docs/adr/](docs/adr/README.md) for the reasoning behind each decision — including
what's a real implementation today versus a deliberate v1 stub —
[docs/roadmap.md](docs/roadmap.md) for where the codebase stands against the
phased enterprise rollout plan, and [docs/user-journeys.md](docs/user-journeys.md)
for how an agent developer, platform engineer, registry operator, or compliance
reviewer actually uses it.

## Status

Early foundation, not production-ready. In particular:

- **Identity defaults to a stub.** `OAC_IDENTITY_MODE=header` (the default) trusts an
  `X-Spiffe-ID` header rather than performing real SPIFFE/SPIRE attestation — only
  safe behind a network boundary that has already authenticated the caller.
  `OAC_IDENTITY_MODE=jwt-svid` cryptographically validates a SPIFFE JWT-SVID bearer
  token (the shape SPIRE issues) but still needs an actual SPIRE deployment to be a
  full production path — see [ADR-0005](docs/adr/0005-workload-identity-via-spiffe-stubbed-in-v1.md).
  `OAC_IDENTITY_MODE=oidc-jwks` validates a real access token from Okta or Microsoft
  Entra ID against its published JWKS — see
  [ADR-0010](docs/adr/0010-oidc-jwks-identity-for-okta-and-entra.md).
- **Token exchange defaults to a stub**, with real Okta-compatible (RFC 8693) and
  Microsoft Entra (OBO) adapters available via `OAC_TOKEN_EXCHANGE_MODE` — see
  [ADR-0004](docs/adr/0004-mcp-as-the-v1-protocol-surface.md).
- **The ledger's signing key defaults to in-process** (regenerated on restart) unless
  you inject your own `ReceiptSigner`; there's no KMS/HSM adapter yet. Chain state
  itself, however, is durable and replica-safe once `OAC_DATABASE_URL` is set — see
  [ADR-0003](docs/adr/0003-ed25519-hash-chained-audit-ledger.md) and
  [ADR-0009](docs/adr/0009-postgres-persistence-and-redis-caching.md).
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
├── application/       # GovernedExecutionService — the transport-agnostic use case
├── adapters/            # concrete implementations of each port (OPA, ledger, identity, db, ...)
└── gateway/               # FastAPI app: routes + dependency wiring, no policy/crypto logic
```

| Port | Default adapter | Also available (settings-selected) |
|---|---|---|
| `PolicyEngine` | Open Policy Agent (Rego), `policies/mcp_authz.rego` | — |
| `IdentityProvider` | header-trusting stub | JWT-SVID validation, OIDC/JWKS (Okta, Entra ID) |
| `AgentRegistry` | file (`registry/agents.yaml`, git-reviewed) | Postgres (`oac.agents`), optionally Redis-cached |
| `Ledger` | in-process Ed25519 hash-chained receipts | Postgres-backed, replica-safe chain (`oac.execution_receipts`) |
| `TokenExchange` | stub | RFC 8693 (Okta-compatible), Microsoft Entra OBO — optionally Redis-cached to each token's own expiry |
| `MCPUpstream` | HTTP forward to a downstream MCP server | — |
| `AuditExporter` | stdout/log | — |

## Development

```bash
make install      # poetry install --all-extras
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

## Persistence & caching (optional)

Unset by default — the gateway runs with the in-process ledger and file registry,
zero extra infrastructure. The Postgres/Redis stack is an **optional extra**
(`pip install 'openagent-control[persistence]'` / `poetry install --extras persistence`)
and is lazy-imported, so the default deployment doesn't pay its ~20MB of resident
memory or its install footprint. Set `OAC_DATABASE_URL` to switch the registry and
ledger to Postgres (own `oac` schema, migrated via Alembic), and `OAC_REDIS_URL` to
cache registry lookups (30s TTL) and brokered tokens (capped at each token's own
expiry). See [ADR-0009](docs/adr/0009-postgres-persistence-and-redis-caching.md).

```bash
make up-persistent               # docker compose --profile persistence: + postgres, redis
make db-upgrade                  # alembic upgrade head against OAC_DATABASE_URL
```

To point at your own Postgres instance instead: `export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/db`
then `make db-upgrade`.

## Examples

- [examples/langgraph_governed_agent/](examples/langgraph_governed_agent/README.md) —
  a deterministic, zero-API-key demo of a LangGraph agent whose tool calls are
  allowed, denied, and cryptographically receipted by the gateway:

  ```bash
  poetry install --with examples
  make up
  poetry run python -m examples.langgraph_governed_agent.demo
  ```

- [examples/oidc_identity_demo/](examples/oidc_identity_demo/README.md) — the
  gateway authenticating agents with real Okta/Entra-shaped OIDC access tokens
  (signature, audience, and orphan-agent checks), fully offline against a mock
  IdP:

  ```bash
  poetry run python -m examples.oidc_identity_demo.demo
  ```
