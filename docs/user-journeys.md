# User Journeys

What each person actually does, in order, against what is real in the codebase
today. See [roadmap.md](roadmap.md) for what is still stubbed, and
[adr/](adr/README.md) for why each decision was made.

- [Day 0 — 10 minutes to your first governed call](#day-0--10-minutes-to-your-first-governed-call)
- [Day 1 — govern an agent that already runs in production](#day-1--govern-an-agent-that-already-runs-in-production)
- [Day 2 — stand it up for the org](#day-2--stand-it-up-for-the-org)
- [Day N — operate it: inventory, access, audit](#day-n--operate-it-inventory-access-audit)
- [The compliance reviewer's job](#the-compliance-reviewers-job)
- [What is real vs. still a stub](#what-is-real-vs-still-a-stub)

---

## Day 0 — 10 minutes to your first governed call

No cloud tenant, no API keys, no IdP.

```bash
pip install openagent-control
openagent-control init ./oac              # starter registry + Rego policy
export OAC_REGISTRY_PATH=./oac/agents.yaml
opa run --server ./oac/policies &
openagent-control doctor                  # exits non-zero if it would not serve
openagent-control serve
```

`doctor` runs exactly the checks `GET /readyz` runs, so the CLI cannot bless a
deployment the load balancer then refuses.

Then govern a function you already have:

```python
from openagent_control.sdk import GovernedClient, governed

oac = GovernedClient("http://localhost:8000", spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

@governed(oac)
def read_query(quarter: str) -> dict:
    return db.query(quarter)
```

`spiffe_id=` works because the default `OAC_IDENTITY_MODE=header` trusts a
header — **a dev stub** ([ADR-0005](adr/0005-workload-identity-via-spiffe-stubbed-in-v1.md)).
Day 2 replaces it with a real token and nothing else in your code changes.

The starter registry is **empty**, so this call is denied and receipted as an
orphan until you register the agent. That is deliberate: a fresh install
trusts nothing.

**To see the whole thing working end to end, with nothing stubbed:**

```bash
brew install opa && poetry install --with examples
poetry run python -m examples.enterprise_scenario.scenario
```

Real OIDC identity, real OPA, real RFC 8693 credential brokering, a real MCP
server over real SQLite — including the demonstration that an agent bypassing
the gateway is refused by the upstream, because its token is scoped to the
gateway and not to the API. That is the part to show a skeptical reviewer.

---

## Day 1 — govern an agent that already runs in production

**Goal:** identity, policy, and a signed audit trail on an existing agent,
without moving its tool code anywhere.

Pick the integration shape that matches where your tools live:

| Your situation | Use | What changes in your code |
|---|---|---|
| Tool functions live in your agent's own process | `@governed(oac)` | One decorator per tool |
| Tools already behind an MCP server | Point the MCP client at the gateway | One URL |
| LangChain / LangGraph agent | `sdk.langchain.govern(...)` / `proxied_tools(oac)` | Your tool list |
| CrewAI / Strands / Google ADK agent | `@governed(oac)` under/around the framework's own tool decorator | One decorator per tool |

```python
from langchain.agents import create_agent
from openagent_control.sdk.langchain import govern, proxied_tools

tools = [govern(update_account, oac), *proxied_tools(oac)]   # your fn + MCP-hosted tools
agent = create_agent(model="anthropic:claude-sonnet-4-6", tools=tools)
```

Full runnable version — real gateway, a real ALLOW, a real DENY, real
receipts — in [`examples/langgraph_governed_agent/`](../examples/langgraph_governed_agent/README.md).
Same, for [CrewAI](../examples/crewai_governed_agent/README.md),
[Google ADK](../examples/google_adk_governed_agent/README.md), and
[Strands](../examples/strands_governed_agent/README.md) — `@governed` has no
LangChain import in it, so it composes with any framework's own tool
decorator the same way, verified against real installs of each.

Denials come back as tool *output* (`BLOCKED: … Stop execution and request user
approval.`), so the model reads them and halts instead of retry-looping. An
exception there would end the graph run instead
([ADR-0017](adr/0017-client-sdk-and-authorize-only-endpoint.md)).

**Register the agent** — until you do, it is refused and receipted as an orphan:

```yaml
- spiffe_id: spiffe://corp.net/ns/finance/agent/invoice-bot
  display_name: Invoice Bot
  owner: alice@corp.net
  risk_tier: medium
  status: active
  granted_tools: [read_query]
```

`granted_tools` is the allowlist and it is sufficient on its own — policy
guardrails only *narrow* it, never constitute it
([ADR-0016](adr/0016-multi-upstream-routing-and-listing-projection.md)). The
agent's `tools/list` is filtered to exactly these, so it never discovers a tool
a call would then be denied for.

A grant can also carry its own terms, for the tools that don't warrant the
agent's blanket access — `update_record` needs a real approver behind it,
`read_query` doesn't:

```yaml
granted_tools:
  - read_query
  - name: update_record
    required_roles: [finance-approver]  # only a delegated call from a human
                                         # holding this role may trigger it
```

Enforced by the shipped policy itself, no extra Rego required
([ADR-0021](adr/0021-per-grant-metadata-risk-tier-approval-required-roles.md)).

**Know what this shape does and does not buy you.** `@governed` gives identity,
policy and audit — but the agent runs the tool with the credential it already
holds, so it *could* bypass the SDK and call the target directly. The proxy
path is the stronger guarantee, because the agent never holds a credential the
target accepts. The SDK is the on-ramp; the proxy is the end state (ADR-0017).

---

## Day 2 — stand it up for the org

1. **Real workload identity.** `OAC_IDENTITY_MODE=oidc-jwks` validates real
   Okta/Entra/Keycloak access tokens against published JWKS
   ([ADR-0010](adr/0010-oidc-jwks-identity-for-okta-and-entra.md)); `jwt-svid`
   for SPIRE. Your agent code changes only its `Authorization` header:
   ```bash
   export OAC_IDENTITY_MODE=oidc-jwks
   export OAC_OIDC_DISCOVERY_URL="https://{yourOktaDomain}/oauth2/default/.well-known/oauth-authorization-server"
   export OAC_OIDC_AUDIENCE="api://your-gateway"
   ```
   Entra: `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`.
   Keycloak: `.../realms/{realm}/.well-known/openid-configuration`. Same three
   env vars either way — the gateway fetches discovery once at startup, caches
   the JWKS, and checks signature, issuer, audience, and expiry on every call.

2. **Roll out without blocking anything.** `OAC_DECISION_MODE=observe` records
   and signs what a policy *would* have blocked, with `enforced=false`, and
   forwards the call anyway ([ADR-0012](adr/0012-shadow-mode-for-first-deployment.md)).
   Run it against live traffic, read the would-be denials on the dashboard,
   tighten the policy, then switch to `enforce`. Registry-gate and fail-closed
   denials are never softened by this.

3. **Durable storage**, once a restart or a second replica matters:
   ```bash
   pip install 'openagent-control[persistence]'
   export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/oac
   openagent-control migrate
   ```

4. **Token exchange** (`OAC_TOKEN_EXCHANGE_MODE=rfc8693|entra`) — the scoped,
   short-lived credential the upstream actually accepts, which the agent never
   sees or holds.

5. **Real key custody.** `OAC_SIGNING_KEY_MODE=vault-transit` keeps the
   Ed25519 key inside HashiCorp Vault
   ([ADR-0013](adr/0013-vault-transit-signing-key-custody.md)). The default
   `in-process` key is regenerated on restart — fine for dev, not compliance
   evidence. **Set this on both the gateway and the control plane**, or
   signature verification cannot work across the two processes.

6. **Several MCP servers behind one gateway** — one registry, one policy
   bundle, one audit chain:
   ```bash
   export OAC_MCP_UPSTREAMS='{"finance":"http://finance:8080/mcp","crm":"http://crm:8080/mcp"}'
   ```

7. **Authorize on the acting user, not just the agent.** For delegated calls,
   `OAC_SUBJECT_VERIFICATION_MODE=oidc-jwks` verifies the human's own token and
   exposes their id, roles and scopes to policy
   ([ADR-0019](adr/0019-sponsorship-is-approval-authorization-comes-from-the-user.md)).
   Sponsorship records *who approved*; this decides *what is permitted*:

   ```rego
   guardrail_violation("update_record", _) if {
       input.subject == null                            # no user authority at all
   }
   guardrail_violation("update_record", _) if {
       input.subject != null
       not "finance-approver" in input.subject.roles    # user isn't entitled
   }
   ```

   Write the null case explicitly. In Rego, `not "x" in null.roles` is
   *undefined*, not true — so an entitlement rule written the obvious way
   silently fails to fire for autonomous calls.

---

## Day N — operate it: inventory, access, audit

```bash
export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/oac
export OAC_CONTROL_PLANE_API_KEY=$(openssl rand -hex 32)
openagent-control serve-control-plane --port 8001
```

Open **`http://localhost:8001/`** and sign in with that credential. The
dashboard is a separate process from the enforcing gateway: it never imports
`GovernedExecutionService`, the policy engine, or the MCP client, and it holds
only the receipt-signing *public* key — it cannot forge a receipt or bypass
policy ([ADR-0014](adr/0014-control-plane-api-and-dashboard.md)).

**Two gotchas that produce an empty-looking dashboard, not an error:**
- The dashboard reads **only** from Postgres. If the gateway is still running
  the default in-process ledger (no `OAC_DATABASE_URL` on the *gateway*), every
  call it handles is real and receipted, but invisible here — `fleet/activity`
  will report `total_calls: 0` forever. Both processes need `OAC_DATABASE_URL`
  pointed at the same database.
- A call from an agent that isn't yet in `oac.agents` is refused before OPA is
  even consulted, so it never shows up as "denied" here either. Register it
  first — `POST /api/v1/agents` with the same operator bearer token, not a
  direct database write, so the `oac.operator_actions` audit trail records who
  added it:
  ```bash
  curl -X POST http://localhost:8001/api/v1/agents \
    -H "Authorization: Bearer $OAC_CONTROL_PLANE_API_KEY" -H "Content-Type: application/json" \
    -d '{"spiffe_id":"...","display_name":"...","purpose":"...","owner":"...","risk_tier":"medium","granted_tools":["read_query"]}'
  ```

### Inventory — every agent, in one place

The **Registered agents** table is the fleet: identity, display name, owner,
risk tier, granted tools, and status. This is the inventory answer to "how many
agents do we have, who owns them, and what can each one touch" — the question
that is otherwise unanswerable once agents are spread across teams.

Every row's `owner` is the accountable human. An agent with no owner should not
exist; the registry is the authorization boundary, so a registry entry granting
`delete_records` is a standing production grant regardless of what Rego says.

### Enable / disable access

Each row has a **Suspend** / **Activate** button.

- **Suspend** is the kill switch. A suspended agent is denied on its next call
  — before the policy engine is even consulted — and the attempt is still
  receipted. Shadow mode never softens this.
- Revocation latency is one registry cache TTL (`OAC_REGISTRY_CACHE_TTL_SECONDS`,
  30s default) with Redis on, immediate without it.
- **Every mutation writes an `oac.operator_actions` row in the same transaction**
  as the change itself, recording which operator did it. Who suspended an agent,
  and when, is itself auditable.

To change *what* an agent may do rather than whether it runs at all, edit
`granted_tools` — via `PATCH /api/v1/agents/{spiffe_id}` or the registry file.
Treat it like a production access-control change, because it is one.

### What was allowed, what wasn't, and why

- **Allowed / Denied** tiles: the last 24 hours at a glance. Denials counted as
  `shadow` are ones a policy *would* have blocked while running in observe mode
  — the number to watch during a rollout.
- **Busiest agents** and **Why calls were denied**: aggregated over a window you
  choose (1h / 24h / 7d). The denial-reason breakdown is where policy tuning
  starts — a spike in *"Capability not granted"* usually means a registry entry
  is too narrow, not that an agent is misbehaving.
- **Recent decisions**: the last 25 receipts, each with agent, decision, reason,
  and receipt id. This is how you answer "my agent got denied" — by reading its
  receipts, not by guessing at policy intent.

Grouping is by agent and by denial reason, **not by tool**. Receipts store a
payload hash rather than the payload ([ADR-0003](adr/0003-ed25519-hash-chained-audit-ledger.md)),
so *that* a call happened is provable while *what it was* is not readable.
Adding a tool-name column to power a chart would trade a privacy property for a
dashboard feature.

### Prove the record hasn't been tampered with

**Verify chain** walks every receipt, recomputing each hash link and signature.
It reports `Intact` with a count, or `BROKEN` with the first bad sequence id.
It is O(n) over the whole table — a fleet integrity check, not a page-load
refresh.

Everything on the dashboard is also available as JSON at `/api/v1/*` with the
same operator credential — `agents`, `receipts`, `receipts/verify-chain`,
`fleet/summary`, `fleet/activity`. The page has no privileged back channel;
anything it can do, `curl` can do.

---

## The compliance reviewer's job

**Goal:** answer "what did this agent do, and was it authorized?" after the
fact, without trusting the agent's account of events.

1. **Read the chain.** Every decision — allow *and* deny — produces an
   Ed25519-signed receipt hash-chained to its predecessor. Tampering with or
   deleting a past receipt breaks every receipt after it. That is what makes
   this evidence rather than a log line.
2. **Verify independently.** Signatures verify against the public key alone; a
   reviewer need not trust the gateway process, only the key custody. With
   `signing_key_mode=vault-transit` the private key never left Vault.
3. **Reconstruct a decision.** Each receipt carries the identity, the decision,
   the reason, whether it was enforced, and a timestamp.
4. **Search** by agent, decision, enforced flag, or time window via
   `GET /api/v1/receipts`.

**Known limits, stated plainly:**
- The receipt records the *agent*, not the acting human. "Which user was this
  done for" is not answerable from the ledger alone yet (ADR-0019).
- The default `in-process` signing key is regenerated on restart. Until
  `vault-transit` is configured, receipts are a good-faith log, not
  compliance-grade evidence.
- No SOC 2 / EU AI Act export generation. The schema carries the fields such a
  report would need; the pipeline does not exist (Phase 5, 0% built).

---

## What is real vs. still a stub

| Real | Not yet |
|---|---|
| OPA policy, fail-closed denials, shadow mode | Response-side filtering |
| Signed hash-chained receipts, durable, replica-safe | Receipts naming the acting human |
| Vault Transit key custody | — (`in-process` remains the **default**) |
| OIDC/JWKS + JWT-SVID identity; verified subject authorization | SPIRE deployment; `header` mode remains the **default** |
| RFC 8693 / Entra OBO exchange | — (`stub` remains the **default**) |
| Real MCP both directions; many upstreams per gateway | Per-upstream credentials; session pooling |
| Control-plane API + dashboard, operator-action audit | Browser OIDC redirect login |
| SDK: `@governed`, proxy client, LangChain/LangGraph | — |
| — | Human-in-the-loop approvals; sandboxed writes; chargeback export |

The three defaults in bold are the ones to change before calling a deployment
production-grade. `openagent-control doctor` names each of them.
