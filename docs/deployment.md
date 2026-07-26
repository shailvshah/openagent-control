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

## Verifying a release

```bash
make test-packaging     # builds a wheel, installs it into a clean venv,
                        # and runs it from an unrelated working directory
```

That last clause is the point: the rest of the suite runs from the checkout,
where `policies/` and `migrations/` happen to be on disk. This test is what
catches a wheel that ships neither.
