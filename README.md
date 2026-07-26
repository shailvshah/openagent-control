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

See [docs/design.md](https://github.com/shailvshah/openagent-control/blob/main/docs/design.md) for the full design,
[docs/adr/](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/README.md) for the reasoning behind each decision — including
what's a real implementation today versus a deliberate v1 stub —
[docs/roadmap.md](https://github.com/shailvshah/openagent-control/blob/main/docs/roadmap.md) for where the codebase stands against the
phased enterprise rollout plan, and [docs/user-journeys.md](https://github.com/shailvshah/openagent-control/blob/main/docs/user-journeys.md)
for how an agent developer, platform engineer, registry operator, or compliance
reviewer actually uses it, and [docs/deployment.md](https://github.com/shailvshah/openagent-control/blob/main/docs/deployment.md) for
installing and running it.

## Status

Early foundation, not production-ready. In particular:

- **Identity defaults to a stub.** `OAC_IDENTITY_MODE=header` (the default) trusts an
  `X-Spiffe-ID` header rather than performing real SPIFFE/SPIRE attestation — only
  safe behind a network boundary that has already authenticated the caller.
  `OAC_IDENTITY_MODE=jwt-svid` cryptographically validates a SPIFFE JWT-SVID bearer
  token (the shape SPIRE issues) but still needs an actual SPIRE deployment to be a
  full production path — see [ADR-0005](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0005-workload-identity-via-spiffe-stubbed-in-v1.md).
  `OAC_IDENTITY_MODE=oidc-jwks` validates a real access token from Okta or Microsoft
  Entra ID against its published JWKS — see
  [ADR-0010](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0010-oidc-jwks-identity-for-okta-and-entra.md).
- **Token exchange defaults to a stub**, with real Okta-compatible (RFC 8693) and
  Microsoft Entra (OBO) adapters available via `OAC_TOKEN_EXCHANGE_MODE` — see
  [ADR-0004](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0004-mcp-as-the-v1-protocol-surface.md).
- **The ledger's signing key defaults to in-process** (regenerated on restart) unless
  you inject your own `ReceiptSigner`; there's no KMS/HSM adapter yet. Chain state
  itself, however, is durable and replica-safe once `OAC_DATABASE_URL` is set — see
  [ADR-0003](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0003-ed25519-hash-chained-audit-ledger.md) and
  [ADR-0009](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0009-postgres-persistence-and-redis-caching.md).
- No Human-in-the-Loop approval flow, no sidecar/native-SDK deployment pattern, no
  cross-organization (DID/VC) identity yet — tracked in
  [ADR-0001](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0001-hybrid-interception-pattern.md) and
  [ADR-0007](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0007-decentralized-identity-is-a-future-extension-not-v1-scope.md).

## Architecture

Hexagonal (ports & adapters) — see
[ADR-0006](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0006-hexagonal-architecture-for-the-control-plane.md).

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
| `MCPUpstream` | MCP Streamable HTTP via the official MCP SDK (ADR-0011) | plain JSON-RPC over HTTP, for non-MCP internal endpoints |
| `AuditExporter` | stdout/log | — |

## Install

```bash
pip install openagent-control                  # gateway + OPA
pip install 'openagent-control[persistence]'   # + Postgres / Redis support

openagent-control init ./oac                   # starter registry + policy
export OAC_REGISTRY_PATH=./oac/agents.yaml
openagent-control doctor                       # checks every dependency
openagent-control serve
```

Or with containers — `docker compose up` (gateway + OPA),
`--profile persistence` for Postgres/Redis, `--profile demo` for the
walkthrough stack. Full guide: **[docs/deployment.md](https://github.com/shailvshah/openagent-control/blob/main/docs/deployment.md)**.

With no registry configured the gateway starts and **denies every agent** — a
fresh install trusts nothing until you register something.

## Development

```bash
make install      # poetry install --all-extras
make quality      # black --check, ruff, mypy
make test         # pytest with coverage (95% gate)
make check        # quality + test
make up           # docker compose: gateway + OPA
make up-demo      # + demo IdP and governed MCP server
make doctor       # verify config and dependencies
make test-packaging  # build a wheel, install it clean, run it from elsewhere
```

CI runs lint, strict types, tests on 3.11/3.12 against real Postgres, Redis and
OPA, packaging conformance, and the end-to-end scenario on every push and PR;
coverage below 95% fails the build. Releases publish to PyPI from GitHub
Actions via Trusted Publishing (no API token) — see
[docs/releasing.md](https://github.com/shailvshah/openagent-control/blob/main/docs/releasing.md).

Requires Python 3.11+ and [Poetry](https://python-poetry.org/). The integration
tests additionally need the `opa` binary (`brew install opa`); without it they
skip rather than substitute a fake policy engine.

Once running (`make up`), obtain an access token the way a real workload does —
an OAuth 2.0 client-credentials grant — then send a governed tool call:

```bash
TOKEN=$(curl -s -X POST http://localhost:8090/oauth2/v1/token \
  -u finance-invoice-svc:scenario-only-not-a-real-secret \
  -d grant_type=client_credentials | jq -r .access_token)

curl -X POST http://localhost:8000/mcp/v1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_query","arguments":{"quarter":"Q3"}}}'
```

The upstream MCP server validates the short-lived credential the gateway brokers
for it, so the same request sent directly to `localhost:8080` is refused. See
[examples/enterprise_scenario/](https://github.com/shailvshah/openagent-control/blob/main/examples/enterprise_scenario/README.md).

## Persistence & caching (optional)

Unset by default — the gateway runs with the in-process ledger and file registry,
zero extra infrastructure. The Postgres/Redis stack is an **optional extra**
(`pip install 'openagent-control[persistence]'` / `poetry install --extras persistence`)
and is lazy-imported, so the default deployment doesn't pay its ~20MB of resident
memory or its install footprint. Set `OAC_DATABASE_URL` to switch the registry and
ledger to Postgres (own `oac` schema, migrated via Alembic), and `OAC_REDIS_URL` to
cache registry lookups (30s TTL) and brokered tokens (capped at each token's own
expiry). See [ADR-0009](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0009-postgres-persistence-and-redis-caching.md).

```bash
make up-persistent               # docker compose --profile persistence: + postgres, redis
make db-upgrade                  # alembic upgrade head against OAC_DATABASE_URL
```

To point at your own Postgres instance instead: `export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/db`
then `make db-upgrade`.

## Examples

- **[examples/enterprise_scenario/](https://github.com/shailvshah/openagent-control/blob/main/examples/enterprise_scenario/README.md) — the
  full stack with nothing stubbed.** A LangGraph agent, real OIDC/JWKS identity,
  real OPA, real RFC 8693 credential brokering, and a real MCP server over real
  SQLite that validates the brokered credential's signature, audience, and scope.
  It also demonstrates the property that distinguishes a control plane from a
  logging proxy: an agent that **bypasses the gateway is refused by the upstream**,
  because the token it holds is scoped to the gateway, not to the API. The same
  assertions run in CI (`tests/integration/`), so the demo cannot rot.

  ```bash
  brew install opa && poetry install --with examples
  poetry run python -m examples.enterprise_scenario.scenario
  ```

  Its [`keycloak/`](https://github.com/shailvshah/openagent-control/blob/main/examples/enterprise_scenario/keycloak/README.md) suite runs
  the same identity and RFC 8693 adapters against **real Keycloak 26.4** — an
  IdP this repo didn't write, so it can't share our bugs. That check earned its
  keep on the first run: it caught the identity adapter misreading Keycloak's
  service-account `sub` as a human sponsor, which would have 401'd every
  autonomous agent.

  The same is done for the MCP protocol itself against **GitHub's production
  MCP server** (`tests/integration/test_github_mcp_conformance.py`), which is
  how we found that the original upstream adapter did not speak MCP at all —
  see [ADR-0011](https://github.com/shailvshah/openagent-control/blob/main/docs/adr/0011-mcp-streamable-http-via-the-official-sdk.md).

- [examples/langgraph_governed_agent/](https://github.com/shailvshah/openagent-control/blob/main/examples/langgraph_governed_agent/README.md) —
  a deterministic, zero-API-key demo of a LangGraph agent whose tool calls are
  allowed, denied, and cryptographically receipted by the gateway:

  ```bash
  poetry install --with examples
  make up
  poetry run python -m examples.langgraph_governed_agent.demo
  ```

- [examples/oidc_identity_demo/](https://github.com/shailvshah/openagent-control/blob/main/examples/oidc_identity_demo/README.md) — the
  gateway authenticating agents with real Okta/Entra-shaped OIDC access tokens
  (signature, audience, and orphan-agent checks), fully offline against a mock
  IdP:

  ```bash
  poetry run python -m examples.oidc_identity_demo.demo
  ```
