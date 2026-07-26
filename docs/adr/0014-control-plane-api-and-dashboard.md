# ADR-0014: Control-plane API + dashboard

## Status
Accepted

## Context
Per ADR-0009: "there is no admin API to suspend/kill an agent yet — that is
separate scope (an operator surface, not a data-layer concern)." Today,
onboarding or suspending an agent means hand-editing `registry/agents.yaml` or
writing a row into `oac.agents` directly (`docs/user-journeys.md`, "Registry
operator" journey). Neither `AgentRegistry` nor `Ledger`
(`domain/ports.py`) has anything beyond a single-row `lookup` / an
append-only `record` — there is no list, search, create, update, or verify
method anywhere in the codebase. Auditors and operators have no way to search
receipts or confirm the hash chain is intact without a raw SQL session.

`docs/roadmap.md` (critical-path item 7, scoped 2026-07-25) names this as
in-scope: "a separate, self-hosted service — registry CRUD, receipt
search/verify, fleet health — backed by the same Postgres. Deliberately not
merged into the enforcing gateway; it has different auth requirements (an
operator identity, not a workload identity) and a compromise there must not
be a path to forging receipts or bypassing policy."

## Decision

### A separate process, not a gateway route
`openagent-control serve-control-plane` runs a second FastAPI app
(`control_plane/app.py`), sharing the same Postgres as the gateway but never
importing `GovernedExecutionService`, `PolicyEngine`, `MCPUpstream`, or
`TokenExchange`. A vulnerability in the control plane's JSON parsing or auth
therefore has no path to the policy-evaluation or credential-brokering code
at all — the two services don't share a process, only a database.

### Security boundary: it can read signatures, never produce them
The control plane never constructs anything capable of `.sign()`. It fetches
the same public key the gateway's `Signer` would expose
(`adapters/ledger/signing.py`), but only ever holds the bare
`Ed25519PublicKey` — there is no code path in `control_plane/` capable of
producing a valid signature, even under full compromise of the process.

Receipts are also never written by this service. `PostgresLedger`
(`adapters/ledger/postgres.py`) implements `Ledger.record` and is the only
thing that can write `oac.execution_receipts`; the control plane instead gets
a deliberately separate, read-only adapter, `PostgresReceiptQuery`
(`adapters/ledger/postgres_query.py`), so it is structurally impossible for
the control-plane container to hold a reference to anything with a
`.record()` method. This is the same reasoning as ADR-0013's `Signer`
Protocol being narrower than "holds a private key" — narrow the capability a
component holds to exactly what it needs, not to what's convenient to wire.

`oac.agents` is the one table this service both reads and writes — that's its
whole purpose. Recommended (not enforced in code, same posture as Vault HA
being out of scope per ADR-0013): grant the control plane's Postgres role
`SELECT`-only on `execution_receipts`/`chain_state` and
`SELECT`/`INSERT`/`UPDATE` on `agents`, for defense in depth beyond the
application-level boundary above.

### New ports, not extended existing ones
`AgentRegistry` and `Ledger` stay exactly as they are — single-method,
gateway-hot-path ports. Two new ports are added instead:

```python
class AgentDirectory(Protocol):
    async def list_agents(self, *, status: AgentStatus | None = None) -> list[RegisteredAgent]: ...
    async def create(self, agent: RegisteredAgent) -> RegisteredAgent: ...
    async def update(self, spiffe_id: str, patch: AgentPatch) -> RegisteredAgent: ...
    async def set_status(self, spiffe_id: str, status: AgentStatus) -> RegisteredAgent: ...

class ReceiptQuery(Protocol):
    async def search(self, *, spiffe_id, decision, enforced, since, until, limit, offset) -> list[ExecutionReceipt]: ...
    async def get(self, sequence_id: str) -> ExecutionReceipt | None: ...
    async def verify_chain(self) -> ChainVerificationResult: ...
```

Bloating `AgentRegistry` itself with list/create/verify methods would force
every existing adapter — including `CachingAgentRegistry`, which only wraps
another `AgentRegistry` for its one method — to grow methods that make no
sense for a cache decorator. `PostgresAgentRegistry` implements both
`AgentRegistry` and `AgentDirectory` on the same class (same table, same
`session_factory`); Python's `Protocol`s are structural, so this needs no
special declaration. `FileAgentRegistry` and `CachingAgentRegistry` implement
only `AgentRegistry`, unchanged — the control plane requires
`OAC_DATABASE_URL` to be set (see below), so `AgentDirectory` only ever needs
one real implementation.

### Operator identity, not workload identity
A new `OperatorIdentity` port, with the same "a real dev-stub plus a real
enterprise adapter" shape as `identity_mode`/`token_exchange_mode`:
- `ApiKeyOperatorAuth` — a static bearer token
  (`OAC_CONTROL_PLANE_API_KEY`), for direct API/script/CI use. Low-ceremony
  default, same posture as `identity_mode=header`.
- `OidcOperatorAuth` — reuses the discovery/JWKS-fetch pattern from
  `OidcJwksIdentityProvider` (ADR-0010) but checks a configurable role/group
  claim instead of deriving a workload identity: this answers "is this human
  allowed to operate the control plane," not "what is this workload."

The dashboard SPA additionally gets a browser-appropriate login: an OIDC
Authorization Code + PKCE flow issuing a signed, `httponly` session cookie,
layered on top of `OidcOperatorAuth` — a human in a browser should not have to
paste a bearer token, but the underlying JSON API's bearer-token auth stays
available underneath for non-browser callers.

### Requires Postgres unconditionally
Unlike the gateway (which has a zero-dependency file-registry/in-memory-ledger
mode for a quick start), the control plane's entire purpose is operating on a
real deployment's persisted data. `build_control_plane_container(settings)`
fails fast at construction if `OAC_DATABASE_URL` is unset, rather than
starting against nothing to manage.

### Every mutation is itself audited
A new table, `oac.operator_actions` (migration `0003`), records
`operator_subject`, `action`, `target_spiffe_id`, and a `detail` blob for
every mutating control-plane call, written in the same transaction as the
`agents` write it accompanies. An admin surface with no record of its own
actions would be a real gap in a project whose whole pitch is auditability.

## Consequences
- Two processes to deploy instead of one, sharing one Postgres instance —
  documented in `docker-compose.yml` as a new `control-plane` service under
  the existing `persistence` profile (it requires Postgres unconditionally).
- `GET /readyz` and `openagent-control doctor` gain a third surface: the
  control plane runs the same `diagnostics.run_all()` the gateway does, so
  the CLI, the gateway's readiness, and the control plane's readiness cannot
  disagree about the state of shared dependencies.
- `ChainVerificationResult` from `verify_chain()` walks the full
  `execution_receipts` table — O(n) — which is fine at this project's
  expected volume but must never be called from a hot path; documented on the
  method itself.
- The dashboard (a Vite/React SPA, built separately and shipped as static
  assets inside the control-plane service) is read-only in this pass: agent
  CRUD and receipt search are exposed via the JSON API the SPA calls, but the
  SPA itself does not yet expose destructive actions beyond what the API
  supports — no additional UI-only capability exists.
- `oac.agents` remains the one table this service writes to. If an operator
  wants database-level enforcement of the boundary above (beyond the
  application-level one this ADR establishes), grant its Postgres role
  reduced privileges as described above — not done automatically, since this
  project does not manage database roles for the operator's Postgres.
- **`verify_chain()` requires `signing_key_mode="vault-transit"` to be
  meaningful across the two processes.** With the default `"in-process"`
  mode (ADR-0003), the gateway and the control plane each generate their own
  independent random Ed25519 key at their own startup — there is no shared
  key for the control plane's public key to verify the gateway's signatures
  against. This is the same underlying limitation SECURITY.md already
  documents for `"in-process"` (a key that regenerates on restart can't
  verify receipts signed before that restart, even within one process); a
  second process makes it unavoidable rather than just a restart edge case.
  Verified directly: `tests/integration/test_control_plane_e2e.py` runs the
  gateway and control plane as separate processes and deliberately does not
  assert `verify_chain()` there, with a comment explaining why;
  `verify_chain()`'s own correctness (given one shared signer) is covered in
  `tests/unit/test_ledger_postgres_query.py`.
