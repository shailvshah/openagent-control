# Governed LangGraph agent — day-1 demo

A LangGraph (`create_agent`) finance bot whose every tool call routes through the
openagent-control gateway: identity-attested, OPA-policy-checked, and recorded as a
signed, hash-chained audit receipt.

The demo is fully deterministic — a scripted chat model drives the agent, so it runs
offline with **zero API keys**.

> This example focuses on the *agent-side integration surface*: what an application
> team writes to route tool calls through the gateway. For the full stack with real
> identity, real credential brokering, and a real upstream that validates the
> brokered credential, see [examples/enterprise_scenario/](../enterprise_scenario/README.md).

## What it shows

1. `read_query` → **ALLOWED** by `policies/mcp_authz.rego` for
   `spiffe://corp.net/ns/finance/agent/invoice-bot`, forwarded to the upstream MCP
   server, results returned to the agent.
2. `salesforce_update_account` → **DENIED** (capability not granted to this
   identity). The gateway returns a semantic error payload; the agent reads
   *"Stop execution and request user approval"* and halts gracefully — no retry loop.
3. Both decisions appear in the gateway log as Ed25519-signed receipts, each chained
   to the previous receipt's hash.

## Run it

`make up` (the default `docker-compose.yml` profile) only starts the gateway
and OPA — no auth server, no MCP upstream. This demo needs the `demo` profile,
which adds the mock IdP (`:8090`) and the governed MCP server (`:8080`):

```bash
poetry install --with examples
make up-demo                   # gateway :8000, OPA :8181, mock IdP+MCP :8090/:8080
poetry run python -m examples.langgraph_governed_agent.demo
docker compose logs gateway | grep audit_receipt   # the signed evidence
```

**Two ways to satisfy identity, pick one.** By default the demo calls
`fetch_agent_token()` and sends a real OAuth bearer token — which the gateway
can only validate if it is *also* running in `OAC_IDENTITY_MODE=oidc-jwks`,
pointed at the mock IdP as issuer. `docker-compose.yml`'s own default for the
`gateway` service is `OAC_IDENTITY_MODE=header`, so the two defaults don't
match out of the box. Either:

```bash
# (a) simplest — matches the compose default, no OIDC wiring needed
export OAC_USE_HEADER_IDENTITY=1
poetry run python -m examples.langgraph_governed_agent.demo
```

```bash
# (b) exercise the real OAuth path — requires recreating the gateway
# container with matching identity config first
OAC_IDENTITY_MODE=oidc-jwks \
OAC_OIDC_DISCOVERY_URL=http://enterprise-backend:8090/.well-known/openid-configuration \
OAC_OIDC_AUDIENCE=api://openagent-control-gateway \
docker compose --profile demo up -d --no-deps gateway
poetry run python -m examples.langgraph_governed_agent.demo
```

`OAC_GATEWAY_URL` and `OAC_TOKEN_URL` override the demo's default addresses if
you're not running the standard compose ports.

### Run it against a real model

By default the agent's "reasoning" is a fixed, scripted script (`scripted_model.py`)
— deterministic output, zero API keys. To have a real model choose its own tool
calls instead, set `ANTHROPIC_API_KEY` and `OAC_DEMO_MODEL`. Copy the repo's
[`.env.local.example`](../../.env.local.example) (also covers every other var
in this walkthrough — identity mode, dashboard persistence) rather than typing
these by hand; the copy (`.env.local`) is gitignored, so a real key never
risks getting committed:

```bash
cp .env.local.example .env.local   # then edit in your real ANTHROPIC_API_KEY

source .venv/bin/activate
set -a; source .env.local; set +a    # exports every var the file defines
poetry run python -m examples.langgraph_governed_agent.demo
```

Same graph, same governed tools, same gateway — only the model differs.
`OAC_DEMO_MODEL` is read directly by `demo.py`; `ANTHROPIC_API_KEY` is read by
`langchain-anthropic` itself (installed by `poetry install --with examples`),
so there is nothing else to configure. Any other `init_chat_model`-supported
provider string works the same way, with that provider's own API key env var.

A real model won't necessarily reproduce the scripted run's exact phrasing or
call order — it may fire both tool calls in parallel, for instance — but the
gateway's ALLOW/DENY outcome per tool is identical either way, since that's
enforced by the registry and OPA, not by the model.

### Watch it on the dashboard

The dashboard is a **separate process** (`control-plane`, port **8001** — not
the gateway's 8000) and only reads from **Postgres**, never the gateway's
in-process ledger. To see this demo's receipts on it:

```bash
# 1. bring up Postgres/Redis alongside the demo profile
docker compose --profile persistence --profile demo up -d postgres redis

# 2. create the oac schema
OAC_DATABASE_URL="postgresql+asyncpg://oac:oac@localhost:5432/oac" \
  poetry run openagent-control migrate

# 3. start the control plane with a local API key, and repoint the gateway
#    at Postgres so new receipts land there instead of only in-process
export OAC_CONTROL_PLANE_API_KEY=$(openssl rand -hex 24)
OAC_DATABASE_URL="postgresql+asyncpg://oac:oac@postgres:5432/oac" \
OAC_CONTROL_PLANE_API_KEY="$OAC_CONTROL_PLANE_API_KEY" \
OAC_CONTROL_PLANE_OPERATOR_AUTH_MODE=api-key \
  docker compose --profile persistence --profile demo up -d --no-deps gateway control-plane

# 4. register the demo's agent through the real API (not a raw SQL insert —
#    this is the same endpoint a production operator would use)
curl -X POST http://localhost:8001/api/v1/agents \
  -H "Authorization: Bearer $OAC_CONTROL_PLANE_API_KEY" -H "Content-Type: application/json" \
  -d '{"spiffe_id":"oidc://http://enterprise-backend:8090/finance-invoice-svc",
       "display_name":"Finance Invoice Service","purpose":"demo",
       "owner":"you@example.com","risk_tier":"medium","granted_tools":["read_query"]}'

poetry run python -m examples.langgraph_governed_agent.demo   # against the gateway from step 3
```

Then open `http://localhost:8001/` and sign in with `$OAC_CONTROL_PLANE_API_KEY`.

### Troubleshooting

These are real failures this exact walkthrough produces if a step is skipped
or run out of order — not hypothetical:

| Symptom | Cause | Fix |
|---|---|---|
| `httpx.ConnectError: Connection refused` fetching the token | `make up` was used instead of `make up-demo` — no mock IdP on `:8090` | `make up-demo` |
| `GatewayError: ... missing required 'x-spiffe-id' header` | Gateway is in `header` mode but the demo sent a bearer token (or vice versa) | Pick one of the two identity options above and match them |
| Tool call denied with `Policy engine unavailable; denied (fail-closed)` | Gateway can't reach OPA over the compose network | Check `docker logs <opa container>` — some OPA image versions default to binding `localhost:8181` inside the container, which only the host (not other containers) can reach; needs `--addr :8181` |
| Dashboard shows `total_calls: 0` after a run | Gateway was still using the in-process ledger, not Postgres | Recreate the gateway with `OAC_DATABASE_URL` set (step 3 above) *before* running the demo |

## Files

- `demo.py` — builds the agent and prints the governed conversation
- `governed_tools.py` — the tool factory: any capability name becomes a LangChain
  tool that proxies through the gateway via `openagent_control.sdk.GovernedClient`
  (this is the piece you'd reuse in a real agent — see
  [ADR-0017](../../docs/adr/0017-client-sdk-and-authorize-only-endpoint.md) and
  `openagent_control.sdk.langchain` for the equivalent auto-discovery helper,
  `proxied_tools()`, when you don't need to reach an ungranted tool by name)
- `scripted_model.py` — deterministic stand-in model, used unless `OAC_DEMO_MODEL`
  is set (see "Run it against a real model" above)
