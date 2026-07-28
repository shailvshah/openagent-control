# openagent-control

**Your AI agents run on static API keys nobody can revoke, scope, or audit.
This puts identity, policy, and a signed audit trail in front of every tool call
they make — in three lines.**

```python
from openagent_control.sdk import GovernedClient, governed

oac = GovernedClient("https://gateway.corp.net", token=agent_token)

@governed(oac)                                  # ← the only change
def update_account(account_id: str, credit_limit: float) -> dict:
    return salesforce.update(account_id, credit_limit=credit_limit)
```

Now that function **cannot run** unless policy allows it, and every attempt —
allowed or denied — leaves an Ed25519-signed, hash-chained receipt.

```
>>> update_account("ACC-1", 50_000)
ToolCallDenied: 'update_account' denied by policy — Tool arguments exceed
authorized thresholds

    Stop execution and request user approval.
```

---

## Why this exists

An agent with a hardcoded API key is a user account with no manager. You cannot
say "this agent may read invoices but not write them", cannot revoke it without
rotating a secret shared by six services, and cannot prove to an auditor what it
did last Tuesday.

`openagent-control` sits between an agent and the tools it calls and enforces
three things on **every** call:

| | |
|---|---|
| **Identity** | The agent is a workload with its own identity (SPIFFE, or an Okta/Entra/Keycloak access token) — not a shared secret. |
| **Policy** | Every call is evaluated against explicit policy (Open Policy Agent) *before* it reaches the target system. On allow, a **scoped, short-lived credential** is brokered for that one call. |
| **Audit** | Every decision produces an Ed25519-signed, hash-chained receipt. A reviewer can prove after the fact what was authorized and what ran. |

The property that makes it a control plane rather than a logging proxy: **an
agent that routes around the gateway is refused by the target system**, because
the token it holds is scoped to the gateway, not to the API. That is
demonstrated end-to-end in [`examples/enterprise_scenario/`](examples/enterprise_scenario/README.md),
not asserted.

## Three ways in, pick the one that fits

**1. Decorate what you already have** — your tool code stays put, the gateway
decides whether it runs. Best for an agent already in production.

```python
@governed(oac)
def update_account(...): ...
```

**2. Point an MCP client at it** — the gateway is a real MCP server
(Streamable HTTP, handshake, SSE). Change one URL.

```python
async with streamable_http_client("https://gateway.corp.net/mcp/") as streams: ...
```

**3. LangChain / LangGraph** — governed tools that drop straight into
`create_agent`. Denials come back as tool *output*, not an exception, so the
model reads "stop and request approval" and halts instead of retry-looping.

```python
from langchain.agents import create_agent
from openagent_control.sdk.langchain import govern, proxied_tools

tools = [govern(update_account, oac), *proxied_tools(oac)]  # your fn + MCP-hosted tools
agent = create_agent(model="anthropic:claude-sonnet-4-6", tools=tools)
```

Full working example, real gateway, real denial, real receipt:
[`examples/langgraph_governed_agent/`](examples/langgraph_governed_agent/README.md).

Whichever you pick, the agent only ever sees the tools its registry record
grants — no discovering a tool that a call would then be denied for.

## Real identity, no code changes

The agent's `token=` is just its normal OAuth access token — swapping identity
providers is a config change, not a rewrite:

```bash
export OAC_IDENTITY_MODE=oidc-jwks
export OAC_OIDC_DISCOVERY_URL="https://{yourOktaDomain}/oauth2/default/.well-known/oauth-authorization-server"
export OAC_OIDC_AUDIENCE="api://your-gateway"
```

Works the same for Entra ID (`https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`)
and Keycloak (`.../realms/{realm}/.well-known/openid-configuration`) — the
gateway fetches the discovery document, caches the JWKS, and validates
signature, issuer, audience, and expiry on every call
([ADR-0010](docs/adr/0010-oidc-jwks-identity-for-okta-and-entra.md)).
Conformance-tested against a real Keycloak realm, not just Okta/Entra's
published shapes on paper. The default (`OAC_IDENTITY_MODE=header`, no
verification) is a dev stub — `openagent-control doctor` flags it.

## Install

```bash
pip install openagent-control

openagent-control init ./oac              # starter registry + policy
export OAC_REGISTRY_PATH=./oac/agents.yaml
openagent-control doctor                  # checks every dependency, exits non-zero
openagent-control serve
```

With no registry configured the gateway starts and **denies every agent** — a
fresh install trusts nothing until you register something.

Or `docker compose up` (gateway + OPA only — `--profile persistence` adds
Postgres/Redis, `--profile demo` adds a mock IdP and MCP upstream). The two
profiles compose: the [LangGraph example](examples/langgraph_governed_agent/README.md)
walks through running with a real model and watching it on the dashboard,
including the identity-mode mismatch you hit if you mix `make up` with a demo
that expects `--profile demo`.
Full guide: **[docs/deployment.md](docs/deployment.md)**.

## See it work, in one command

```bash
brew install opa && poetry install --with examples
poetry run python -m examples.enterprise_scenario.scenario
```

No API keys, no cloud tenant — and nothing in the protocol, crypto, or policy
path is simulated. Real OIDC identity, real OPA, real RFC 8693 credential
brokering, a real MCP server over real SQLite, and a real demonstration that
bypassing the gateway fails.

## Watch the fleet

```bash
openagent-control serve-control-plane --port 8001    # requires OAC_DATABASE_URL
```

A dashboard at `http://localhost:8001/`: which agents exist, what they called,
why calls were denied, and a one-click integrity check over the whole receipt
chain. One static file — no build step, no CDN, works airgapped
([ADR-0018](docs/adr/0018-dashboard-as-one-static-file.md)).

## Status: early foundation, not production-ready

Honest about what is a real implementation and what is a deliberate stub:

| Real today | Still a stub / not built |
|---|---|
| OPA policy evaluation, fail-closed denials, shadow mode | Response-side filtering (Phase 3) |
| Ed25519 hash-chained receipts, durable and replica-safe | Sequence-sealing API |
| Vault Transit key custody (`OAC_SIGNING_KEY_MODE=vault-transit`) | `in-process` is the **default**, and is dev-grade |
| OIDC/JWKS identity (Okta, Entra, Keycloak), JWT-SVID validation | `OAC_IDENTITY_MODE=header` is the **default**, and is a dev stub; no SPIRE deployment |
| RFC 8693 + Entra OBO token exchange | `stub` is the **default** |
| Real MCP transport, both directions; many upstreams behind one gateway | Per-upstream credentials; MCP session pooling |
| Control-plane API + dashboard | Browser OIDC redirect login |
| — | Human-in-the-loop approvals; sandboxed writes; chargeback/compliance export |

See [docs/roadmap.md](docs/roadmap.md) for where the codebase stands against the
phased rollout plan, and [docs/adr/](docs/adr/README.md) for the reasoning
behind every decision — including what was tried, disproved by a real test, and
corrected.

## Verified against real systems, never mocks

There is no `unittest.mock` anywhere in this repo. Adapters are tested against
the real thing, because a hand-written double agrees with whatever its author
assumed — and that has caught real bugs here, repeatedly:

- **Real Keycloak 26.4** caught the identity adapter misreading a
  service-account `sub` as a human sponsor, which would have 401'd every
  autonomous agent.
- **GitHub's production MCP server** caught that the upstream adapter did not
  speak MCP at all ([ADR-0011](docs/adr/0011-mcp-streamable-http-via-the-official-sdk.md)).
- **Real LangChain** caught proxied tools shipping no argument schema, so the
  model called them with no arguments ([ADR-0017](docs/adr/0017-client-sdk-and-authorize-only-endpoint.md)).
- **Real OPA** caught the default policy denying any tool without a hand-written
  Rego rule — making a registry grant silently insufficient
  ([ADR-0016](docs/adr/0016-multi-upstream-routing-and-listing-projection.md)).

Integration tests skip when a real dependency is absent rather than substituting
a fake.

## Architecture

Hexagonal (ports & adapters) — [ADR-0006](docs/adr/0006-hexagonal-architecture-for-the-control-plane.md).

```
src/openagent_control/
├── domain/         # pure models + Protocol ports — no I/O, no framework imports
├── application/    # GovernedExecutionService — the transport-agnostic use case
├── adapters/       # concrete ports (OPA, ledger, identity, db, MCP, ...)
├── gateway/        # FastAPI app: routes + wiring, no policy/crypto logic
├── control_plane/  # separate service: registry CRUD, receipts, dashboard
└── sdk/            # the client: GovernedClient, @governed, LangChain
```

| Port | Default adapter | Also available |
|---|---|---|
| `PolicyEngine` | Open Policy Agent (Rego) | — |
| `IdentityProvider` | header stub | JWT-SVID, OIDC/JWKS (Okta, Entra, Keycloak) |
| `AgentRegistry` | file (`registry/agents.yaml`) | Postgres, optionally Redis-cached |
| `Ledger` | in-process Ed25519 chain | Postgres-backed, replica-safe |
| `Signer` | in-process key | HashiCorp Vault Transit |
| `TokenExchange` | stub | RFC 8693 (Okta), Entra OBO, Redis-cached |
| `MCPUpstream` | MCP Streamable HTTP (official SDK) | raw JSON-RPC; routing across many upstreams |

## Development

```bash
make install      # poetry install --all-extras
make check        # black, ruff, mypy --strict, pytest (95% coverage gate)
make up           # docker compose: gateway + OPA
make test-packaging  # build a wheel, install it clean, run it from elsewhere
```

CI runs lint, strict types, and tests on 3.11/3.12 against real Postgres, Redis
and OPA on every push. Requires Python 3.11+ and [Poetry](https://python-poetry.org/);
integration tests need the `opa` binary (`brew install opa`).

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). Apache-2.0.
