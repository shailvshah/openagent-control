# Deployment

Two supported ways to run `openagent-control`: a **pip install** (a process you
supervise, or a library you embed) and **docker compose**. Both run the same
package — the container installs the built wheel rather than copying source, so
an image cannot pass while the published package is broken.

> **Why self-hosted?** The gateway holds the token-exchange client secret, sits
> in the network path to your internal tools, and signs the audit receipts. All
> three are reasons it belongs inside your trust boundary rather than behind
> someone else's API.

## pip

```bash
pip install openagent-control                  # gateway + OPA only
pip install 'openagent-control[persistence]'   # + Postgres and Redis support
```

That puts `openagent-control` on your PATH:

| Command | What it does |
|---|---|
| `openagent-control init <dir>` | Writes a starter registry and the default Rego policy for you to customise |
| `openagent-control migrate` | Creates/upgrades the `oac` schema (requires `OAC_DATABASE_URL`) |
| `openagent-control doctor` | Checks every dependency and exits non-zero if the gateway would not serve |
| `openagent-control serve` | Runs the gateway |
| `openagent-control serve-control-plane` | Runs the control-plane API + dashboard (ADR-0014, ADR-0018) — a separate process, requires `OAC_DATABASE_URL` |

### Minimal run

```bash
openagent-control init ./oac
export OAC_REGISTRY_PATH=./oac/agents.yaml
opa run --server ./oac/policies &          # or point OAC_OPA_URL at your own OPA
openagent-control doctor && openagent-control serve
```

With no `OAC_REGISTRY_PATH`, the gateway falls back to a bundled **empty**
registry — it starts and denies every agent, rather than trusting an identity
you never registered. `doctor` says so explicitly.

### With persistence

Setting `OAC_DATABASE_URL` is **not sufficient on its own** — the schema has to
exist. An un-migrated database connects perfectly and then fails every query:

```bash
pip install 'openagent-control[persistence]'
export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/oac
export OAC_REDIS_URL=redis://host:6379/0        # optional cache
openagent-control migrate
openagent-control doctor                        # verifies schema is at head
openagent-control serve
```

Postgres is the supported backend (ADR-0009): the schema lives in a dedicated
`oac` namespace, which SQLite and MySQL have no equivalent for. `migrate` says
so rather than failing inside Alembic.

### Embedding it instead

`GovernedExecutionService` is transport-agnostic, so you can govern tool calls
in-process without running a proxy at all — the native-SDK pattern from
ADR-0001. `openagent_control.gateway.dependencies.build_container` wires the
adapters from the same `Settings`.

## docker compose

```bash
docker compose up                            # gateway + OPA
docker compose --profile persistence up      # + Postgres + Redis
docker compose --profile demo up             # + a demo IdP and MCP server
```

The **demo profile is not a deployment**. It ships a hardcoded client secret and
an authorization server that mints a token for anyone who asks — fine for a
walkthrough, a serious foot-gun anywhere reachable. See
[examples/enterprise_scenario/](../examples/enterprise_scenario/README.md).

After starting the persistence profile, run the migration once:

```bash
docker compose --profile persistence up -d postgres redis
OAC_DATABASE_URL=postgresql+asyncpg://oac:oac@localhost:5432/oac make db-upgrade
```

The registry is mounted **read-only** so the gateway cannot rewrite its own
allowlist, and OPA mounts the policy directory straight out of the package so
the container and a pip install evaluate identical Rego.

## MCP endpoints

| Endpoint | Protocol | Use for |
|---|---|---|
| `POST /mcp` (also GET/DELETE for the session lifecycle) | Real MCP Streamable HTTP (handshake, session, SSE) via the official SDK | A genuine MCP client — the SDK's own client, LangChain's MCP adapter, Claude, etc. See [ADR-0015](adr/0015-real-mcp-ingress-transport.md). |
| `POST /mcp/v1` | Bare JSON-RPC over HTTPS, not real MCP transport | An internal caller that already speaks plain JSON-RPC and doesn't need real MCP semantics (ADR-0011's `raw-jsonrpc` framing, now stated symmetrically for the incoming side). |

Both point at the same `GovernedExecutionService` — identical governance,
different transport. `/mcp` runs stateless (a fresh transport per request,
no session-affinity requirement across replicas — a deliberate v1 trade-off,
see ADR-0015).

`POST /api/v1/authorize` is the third surface, and the only one that does not
proxy: it decides and receipts a call without running it, so an agent can keep
its tool code where it is and ask "may I?" first. That is what
`openagent_control.sdk`'s `@governed` decorator calls — see
[ADR-0017](adr/0017-client-sdk-and-authorize-only-endpoint.md), including the
security difference this trades away (no brokered credential, so it cannot
make gateway bypass impossible the way the proxy path does).

### Several MCP servers behind one gateway

```bash
export OAC_MCP_UPSTREAMS='{"finance":"http://finance:8080/mcp","crm":"http://crm:8080/mcp"}'
```

`tools/list` fans out and merges; `tools/call` routes to whichever upstream
advertised the tool. Key order matters — on a tool-name collision the first
listed wins. Unset (the default), `OAC_MCP_UPSTREAM_URL` is used exactly as
before. See [ADR-0016](adr/0016-multi-upstream-routing-and-listing-projection.md).

A listing is also projected down to each agent's registry grants, so an agent
never discovers a tool it would only be denied for calling.

## Health endpoints

| Endpoint | Meaning | Use for |
|---|---|---|
| `GET /healthz` | The process is alive. Deliberately shallow. | Liveness probe |
| `GET /readyz` | OPA reachable, schema at head, Redis reachable, registry readable. **503** if any fails. | Readiness probe, load balancer |

`/readyz` runs exactly the checks `openagent-control doctor` runs, so the CLI
cannot bless a deployment the orchestrator then refuses to route to. Keep
liveness on `/healthz`: restarting a container will not fix an un-migrated
database, and a deep liveness probe turns one bad dependency into a crash loop.

## Configuration

Every setting is an `OAC_`-prefixed environment variable — see
[`config.py`](../src/openagent_control/config.py). The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `OAC_REGISTRY_PATH` | bundled empty registry | The authorization boundary (ADR-0008) |
| `OAC_OPA_URL` | `http://localhost:8181/...` | Policy engine |
| `OAC_IDENTITY_MODE` | `header` | **`header` is a dev stub** — use `oidc-jwks` or `jwt-svid` in production |
| `OAC_MCP_UPSTREAM_MODE` | `streamable-http` | Real MCP transport (ADR-0011); `raw-jsonrpc` only for non-MCP endpoints |
| `OAC_DATABASE_URL` | unset | Postgres; requires the `persistence` extra **and** `migrate` |
| `OAC_REDIS_URL` | unset | Cache only, no schema |
| `OAC_TOKEN_EXCHANGE_MODE` | `stub` | `rfc8693` (Okta/Keycloak) or `entra` |
| `OAC_DECISION_MODE` | `enforce` | `observe` forwards a policy DENY instead of blocking it, and receipts it with `enforced=false` — see [ADR-0012](adr/0012-shadow-mode-for-first-deployment.md). For a first production rollout, before trusting a policy to actually block anything. |
| `OAC_SIGNING_KEY_MODE` | `in-process` | `vault-transit` signs receipts via HashiCorp Vault (`OAC_VAULT_URL`, `OAC_VAULT_TOKEN`, `OAC_VAULT_TRANSIT_KEY_NAME`) — the private key never leaves Vault. See [ADR-0013](adr/0013-vault-transit-signing-key-custody.md); `in-process` is a dev-grade default, not compliance-grade custody. |
| `OAC_OTEL_ENABLED` | `false` | Emits spans through the governed-execution path (identify, registry lookup, policy evaluate, credential broker, forward) via OTLP/HTTP. Off by default: instrumentation always runs (a no-op tracer costs nothing), only exporting is opt-in. |
| `OAC_OTEL_EXPORTER_ENDPOINT` | `http://localhost:4318/v1/traces` | OTLP/HTTP traces endpoint — any collector or vendor OTLP ingest, not tied to a specific backend. |
| `OAC_OTEL_SERVICE_NAME` | `openagent-control` | `service.name` resource attribute on exported spans. |
| `OAC_CONTROL_PLANE_OPERATOR_AUTH_MODE` | `api-key` | `api-key` (a static `OAC_CONTROL_PLANE_API_KEY`) or `oidc-jwks` (a real operator's OIDC access token, checked against a role/group claim). See [ADR-0014](adr/0014-control-plane-api-and-dashboard.md). |
| `OAC_CONTROL_PLANE_API_KEY` | unset | Required when `operator_auth_mode=api-key`. |
| `OAC_CONTROL_PLANE_OIDC_ROLE_CLAIM` | `roles` | Which claim carries roles/groups: Entra `roles`, Okta a custom claim you configure, Keycloak `realm_access.roles` (dotted path into a nested object). |
| `OAC_CONTROL_PLANE_OIDC_REQUIRED_ROLE` | `oac-operator` | The role/group value the claim above must contain. |

### Control-plane API + dashboard

```bash
export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/oac   # required — no zero-dependency mode
export OAC_CONTROL_PLANE_API_KEY=$(openssl rand -hex 32)
openagent-control serve-control-plane --port 8001
```

The dashboard is then at `http://localhost:8001/` — fleet counts, the agent
table with suspend/activate, recent decisions, and a ledger-integrity check.
It is one static file with no build step and no CDN, so it works in an
airgapped deployment; sign in by pasting the same operator credential the API
takes. See [ADR-0018](adr/0018-dashboard-as-one-static-file.md), including what
it deliberately does *not* do (no browser OIDC redirect login yet).

A separate process from the gateway (ADR-0014): registry CRUD, receipt
search/verify, and fleet health, sharing the same Postgres. It never imports
`GovernedExecutionService`, the policy engine, or the MCP upstream client — a
compromise here has no path to those. It also never holds anything capable of
signing a receipt, only the public key, and never writes
`oac.execution_receipts` — see the ADR for the full security-boundary
reasoning.

### Vault-backed receipt signing

```bash
vault secrets enable transit
vault write -f transit/keys/oac-receipt-signer type=ed25519   # Ed25519 only — AWS KMS/Azure Key Vault don't support it, see ADR-0013

export OAC_SIGNING_KEY_MODE=vault-transit
export OAC_VAULT_URL=http://your-vault:8200
export OAC_VAULT_TOKEN=...            # a token scoped to sign/read on this one transit key
export OAC_VAULT_TRANSIT_KEY_NAME=oac-receipt-signer
openagent-control doctor              # reports a public-key fingerprint if reachable
```

An unreachable Vault or a missing transit key fails at startup, the same
posture as the OIDC identity adapter — not a silent per-request signing
failure.

**Required if you run the control plane** (`serve-control-plane`, ADR-0014):
its receipt-search and verify-chain endpoints only produce meaningful
signature verification when the gateway and the control plane share a
signing key. With the default `OAC_SIGNING_KEY_MODE=in-process`, each process
generates its own independent random key at startup — the control plane can
list and search receipts either way, but has no way to verify a signature the
gateway's process produced. Set `OAC_SIGNING_KEY_MODE=vault-transit` on both
services, pointing at the same Vault transit key, to make verification
cross-process-meaningful.

### Tracing

```bash
export OAC_OTEL_ENABLED=true
export OAC_OTEL_EXPORTER_ENDPOINT=http://your-collector:4318/v1/traces
export OAC_OTEL_SERVICE_NAME=openagent-control
```

Every governed call produces one root span (`governed_execution.execute`,
tagged with the tool name and the policy decision) and child spans for
`identify`, `registry.lookup`, `policy_evaluate`, `broker_credential`, and
`forward`. Verified against a real local OpenTelemetry Collector binary
(`tests/integration/test_otel_tracing.py`) — not just the SDK's in-memory
exporter — so the OTLP/HTTP wire format is proven, not assumed. An unreachable
collector never affects request handling: exporting happens off the request
path via a batching background thread.

## Verifying a release

```bash
make test-packaging     # builds a wheel, installs it into a clean venv,
                        # and runs it from an unrelated working directory
```

That last clause is the point: the rest of the suite runs from the checkout,
where `policies/` and `migrations/` happen to be on disk. This test is what
catches a wheel that ships neither.
