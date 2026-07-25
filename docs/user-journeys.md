# User Journeys

Four people touch `openagent-control` in practice, each for a different reason.
This maps what each of them actually does, in what order, against what's real
in the codebase today (see [roadmap.md](roadmap.md) for what's stubbed vs.
production-grade).

1. [Agent developer](#1-agent-developer) — wants their agent's tool calls governed, with minimal glue code
2. [Platform/security engineer](#2-platformsecurity-engineer) — deploys and configures the gateway for an org
3. [Registry operator](#3-registry-operator) — day-to-day: onboard, suspend, review agents
4. [Compliance / audit reviewer](#4-compliance--audit-reviewer) — proves after the fact what happened

---

## 1. Agent developer

**Goal:** point an existing agent's tool calls through the gateway instead of
straight at the target system, without rewriting the agent.

1. Read the [LangGraph example](../examples/langgraph_governed_agent/README.md)
   to see the shape of the integration — the agent's tool-calling node sends a
   normal JSON-RPC `tools/call` to `POST /mcp/v1` instead of invoking the tool
   directly. No SDK to install; it's an HTTP call with an identity header/token.
2. Run it locally with zero config:
   ```bash
   make up                                              # gateway + OPA + mock upstream, file registry
   poetry install --with examples
   poetry run python -m examples.langgraph_governed_agent.demo
   ```
   This proves the golden path — an allowed call, a denied call, and the
   receipt each produces — before touching real infrastructure.
3. Register the agent: add an entry to `registry/agents.yaml` (or the
   Postgres-backed table once persistence is on — see journey 3) with the
   tools it's allowed to call. An agent that calls in without a registry entry
   is refused and receipted as an orphan by design (ADR-0008) — there's no
   implicit trust.
4. Write the Rego policy for what "allowed" means for that agent's risk tier —
   `policies/mcp_authz.rego` is the starting point; capability grants come
   from the registry, argument thresholds live in policy.
5. Swap the identity header for real auth once ready for a shared environment
   — see journey 2, step 2. Nothing else in the agent's code changes; only the
   `Authorization` header / SPIFFE header does.

**What "day 1 value" looks like:** the LangGraph demo runs with no API keys,
no external services, and produces a cryptographically chained audit trail
for both the allowed and denied call — that's the thing to point a skeptical
teammate at first.

---

## 2. Platform/security engineer

**Goal:** stand the gateway up for real, wired to the org's actual identity
provider, policy, and durable storage — the work in journey 1 assumed a
throwaway file registry and a trusted header.

1. **Pick an identity mode** (`OAC_IDENTITY_MODE`) based on what the org
   already runs:
   - `oidc-jwks` — Okta or Microsoft Entra ID already issue access tokens to
     workloads. Point `OAC_OIDC_DISCOVERY_URL` / `OAC_OIDC_AUDIENCE` at the
     tenant; see [ADR-0010](adr/0010-oidc-jwks-identity-for-okta-and-entra.md)
     and run [`examples/oidc_identity_demo/`](../examples/oidc_identity_demo/README.md)
     first against the bundled mock IdP to see the exact three failure/success
     paths (allow, orphan-deny, wrong-audience-401) before pointing at a real
     tenant.
   - `jwt-svid` — SPIRE is already deployed and issues SPIFFE JWT-SVIDs.
   - `header` — dev/demo only; trusts an `X-Spiffe-ID` header outright. Only
     safe behind a boundary that already authenticated the caller
     ([ADR-0005](adr/0005-workload-identity-via-spiffe-stubbed-in-v1.md)).
2. **Turn on durable storage** once file-based registry/in-memory ledger
   won't survive a restart or a second replica:
   ```bash
   export OAC_DATABASE_URL=postgresql+asyncpg://user:pass@host/db
   make db-upgrade        # alembic, creates the oac schema
   export OAC_REDIS_URL=redis://host:6379/0   # optional: caches registry reads + brokered tokens
   ```
   See [ADR-0009](adr/0009-postgres-persistence-and-redis-caching.md). This is
   additive — `poetry install --extras persistence`, otherwise unset and the
   gateway stays zero-dependency.
3. **Wire token exchange** (`OAC_TOKEN_EXCHANGE_MODE`) to whatever mints the
   scoped, short-lived credential the upstream tool actually accepts — RFC
   8693 (Okta-compatible) or Entra's OBO grant. This is the credential the
   agent itself never sees or holds.
4. **Write the org's real policy** in `policies/mcp_authz.rego`, run OPA as
   its own process (`make up` already wires this in docker-compose), and
   decide the enforcement point for rollout — see journey 2a below if this is
   a first deployment into an existing production traffic path.
5. **Deploy.** `Dockerfile` is a multi-stage build; `docker-compose.yml` has
   a `persistence` profile. There's no Helm chart or k8s manifest yet — that's
   an open gap, not a hidden feature.

### 2a. First rollout into live traffic

The plan this project targets assumes a shadow/observe-only phase before
enforcing (see [roadmap.md](roadmap.md) critical path item 1). That toggle
(`decision_mode: enforce|observe`) **does not exist yet** — today every DENY
blocks the call. If rolling out against traffic you don't fully trust your
policy for yet, the honest options are: (a) start with a deliberately
permissive policy and tighten it using the receipts as ground truth, or (b)
wait for the shadow-mode toggle. Don't assume "observe mode" exists because
it's in the roadmap — it isn't built.

---

## 3. Registry operator

**Goal:** the day-to-day job of keeping the Agent Registry accurate — who's
allowed to exist, and what they're allowed to touch.

1. **Onboard an agent:** add a row (`registry/agents.yaml` in file mode, or an
   insert into `oac.agents` in Postgres mode) with `spiffe_id`, `owner`,
   `risk_tier`, `status: active`, and `granted_tools`. Nothing calls through
   the gateway successfully until this exists — that's the zero-orphaned-agents
   guarantee from [ADR-0008](adr/0008-agent-registry-as-declarative-data.md).
2. **Suspend an agent:** flip `status` to `suspended`. In file mode this is a
   git-reviewed YAML edit (deliberately — it's an audit trail of its own). In
   Postgres mode it's a row update; there's **no admin API yet** to do this
   over HTTP (tracked in [roadmap.md](roadmap.md) critical path item 6) — so
   today this means direct DB/file access, and the change takes effect within
   one cache TTL (30s default) if Redis caching is on, immediately otherwise.
3. **Review before merging a registry change:** the registry is the
   authorization boundary, not the policy — a registry entry with
   `granted_tools: [delete_records]` is a real, standing grant regardless of
   what Rego says elsewhere. Treat registry PRs with the scrutiny of a
   production access-control change, because that's what they are.
4. **Investigate a denial:** every decision — allow or deny — produces a
   receipt (see journey 4). An agent complaining "I got denied" is answered by
   reading its own chained receipts, not by guessing at policy intent.

---

## 4. Compliance / audit reviewer

**Goal:** answer "what did this agent actually do, and was it authorized?"
after the fact, without trusting the agent's own account of events.

1. **Read the receipt chain.** Every tool call — allowed or denied — produces
   an Ed25519-signed receipt, hash-chained to the one before it
   ([ADR-0003](adr/0003-ed25519-hash-chained-audit-ledger.md)). Tampering with
   or deleting a past receipt breaks the chain for every receipt after it,
   which is the property that makes this evidence rather than a log line.
2. **Verify a signature independently.** The public key is retrievable from
   the ledger adapter (`ledger.public_key()`); a reviewer doesn't need to
   trust the gateway process itself to verify what it signed, only the key
   custody. **Known gap:** the signing key is generated in-process by default
   (no KMS/HSM adapter yet) — for the receipt to count as real compliance
   evidence rather than a good-faith log, that key needs to come from a
   controlled source. Don't present this as audit-grade until that's closed.
3. **Reconstruct a decision.** Each receipt carries the identity, the tool
   call, the policy decision and reason, and a timestamp — enough to answer
   "was this agent authorized to do this, and who granted it" without asking
   the platform team to remember.
4. **What's not here yet:** SOC 2 / EU AI Act export generation, and
   chargeback/billing integration (Phase 5 in roadmap.md, 0% built). The
   receipt schema carries the fields such an export would need, but no export
   pipeline exists — a reviewer today is reading receipts directly, not
   pulling a report.
