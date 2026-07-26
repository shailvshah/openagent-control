# Enterprise scenario — the full stack, nothing stubbed

A finance agent reads invoices through the control plane. Every component in the
path is real; the only concession to running on a laptop is that the
authorization server listens on localhost instead of an Okta org or Entra tenant.

```
LangGraph agent
   │  Authorization: Bearer <agent's own OIDC access token>
   │  X-Subject-Token: <human sponsor's token>
   ▼
openagent-control gateway  (uvicorn, real port)
   ├─ OIDC/JWKS identity validation   real RS256 signature, live JWKS, aud + iss
   ├─ Agent Registry lookup           real adapter, real YAML
   ├─ OPA policy evaluation           real `opa run --server` over policies/mcp_authz.rego
   ├─ Ed25519 hash-chained receipt    real signing, chained to the previous decision
   └─ RFC 8693 token exchange         real grant, real client authentication
   │  Authorization: Bearer <short-lived token scoped to the MCP server>
   ▼
MCP server  (real JSON-RPC, real SQLite)
   └─ validates signature, issuer, audience, scope — then runs real SQL
```

## What each scenario proves

1. **Delegated read.** `read_query` is granted, so OPA allows it, the gateway
   exchanges the sponsor's token for one scoped to the MCP server, and real rows
   come back from a real SQLite table. The MCP server reports who it served from
   the token's own `sub` and `act.sub` claims — not from anything the caller
   asserted.
2. **Policy denial.** `update_record` is not in the agent's `granted_tools`, so
   the call is denied before any upstream request is made. The invoice is
   provably unchanged afterwards.
3. **Gateway bypass.** The agent calls the MCP server *directly* with its own
   token. That token is cryptographically valid — the gateway just accepted it —
   and it is still refused, because its audience is the gateway, not the API.
   **There is no path to the data that skips governance.** This is the difference
   between a control plane and a logging proxy.
4. **Registry kill-switch.** Suspending the agent in the registry denies the next
   call, with no policy change and no restart.

## Run it

```bash
brew install opa                      # the policy engine is real, not simulated
poetry install --with examples
poetry run python -m examples.enterprise_scenario.scenario
```

Add `OAC_SCENARIO_MODEL=anthropic:claude-sonnet-4-6` (with `ANTHROPIC_API_KEY`)
to run the identical graph against a real LLM choosing its own tool calls; the
default is a scripted model so the demo is deterministic and needs no API key.

The same assertions run in CI as
[`tests/integration/test_enterprise_scenario.py`](../../tests/integration/test_enterprise_scenario.py),
so the demo cannot rot into fiction.

## Files

| File | What it is |
|---|---|
| `authorization_server.py` | Real OAuth 2.0 AS: OIDC discovery, JWKS, `client_credentials` and RFC 8693 token-exchange grants, HTTP Basic client auth |
| `mcp_server.py` | Real MCP JSON-RPC server over real SQLite; validates bearer signature, issuer, audience, and per-tool scope |
| `agent.py` | Real LangGraph agent; tools are HTTP calls to the gateway |
| `harness.py` | Starts real OPA and the real gateway under uvicorn |
| `scenario.py` | Runs the four scenarios above |
| `serve.py` | Runs the AS + MCP server as a long-lived process for `docker compose` |

## What is real, and what is not

| Real | Simulated |
|---|---|
| RS256 signing, JWKS publication and rotation-aware lookup | The IdP is local, not an Okta org or Entra tenant — but see [keycloak/](keycloak/README.md), which runs the same adapters against a real third-party IdP |
| OIDC discovery, `aud`/`iss`/`exp` validation | — |
| RFC 8693 token exchange, incl. client authentication and `act` claims | — |
| OPA policy evaluation (`opa` binary) | — |
| MCP JSON-RPC, bearer validation, scope enforcement, SQL | The invoice data is seeded fixtures |
| Ed25519 hash-chained receipts | The signing key is in-process, not KMS-backed |
| LangGraph agent and graph execution | The model is scripted unless `OAC_SCENARIO_MODEL` is set |

## Pointing this at a real tenant

The gateway configuration is already production-shaped — `scenario.py` sets the
same `Settings` a deployment sets as `OAC_*` environment variables. Swapping in a
real IdP means changing URLs, not code:

```bash
export OAC_IDENTITY_MODE=oidc-jwks
export OAC_OIDC_DISCOVERY_URL="https://{yourOktaDomain}/oauth2/default/.well-known/oauth-authorization-server"
export OAC_OIDC_AUDIENCE="api://your-gateway"
export OAC_TOKEN_EXCHANGE_MODE=rfc8693     # or "entra" for the OBO grant
export OAC_TOKEN_EXCHANGE_URL="https://{yourOktaDomain}/oauth2/default/v1/token"
export OAC_DELEGATED_AUDIENCE="https://your-downstream-api"
```

See the `enterprise-idp-integration` skill for per-provider endpoint shapes and
[ADR-0010](../../docs/adr/0010-oidc-jwks-identity-for-okta-and-entra.md) for the
identity model.
