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

```bash
poetry install --with examples
make up                        # gateway :8000, OPA :8181, governed MCP server :8080
poetry run python -m examples.langgraph_governed_agent.demo
docker compose logs gateway | grep audit_receipt   # the signed evidence
```

The demo first obtains its own access token via an OAuth 2.0 client-credentials
grant (`fetch_agent_token`), because `make up` runs the gateway with real OIDC
identity. `OAC_GATEWAY_URL` and `OAC_TOKEN_URL` override those addresses; set
`OAC_USE_HEADER_IDENTITY=1` to use the `X-Spiffe-ID` dev stub instead, which
requires running the gateway with `OAC_IDENTITY_MODE=header`.

The demo agent talks to the gateway over plain HTTP, so it does not know or care
whether the gateway is backed by the in-process ledger/file registry or by Postgres
+ Redis (ADR-0009) — same output either way. To prove that end-to-end against real
persistence instead of `make up`:

```bash
make up-persistent             # + postgres, redis; set OAC_DATABASE_URL/OAC_REDIS_URL
make db-upgrade                # alembic upgrade head
# seed the demo agent into oac.agents (see registry/agents.yaml for the fields),
# then run the demo exactly as above — identical conversation, receipts land in
# oac.execution_receipts instead of only the gateway's stdout log.
```

## Files

- `demo.py` — builds the agent and prints the governed conversation
- `governed_tools.py` — the tool factory: any capability name becomes a LangChain
  tool that calls the gateway's `/mcp/v1` (this is the piece you'd reuse in a real
  agent)
- `scripted_model.py` — deterministic stand-in model; swap for
  `model="anthropic:claude-sonnet-4-6"` in `demo.py` to run it live
