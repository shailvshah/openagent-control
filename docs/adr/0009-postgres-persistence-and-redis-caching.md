# ADR-0009: Postgres persistence for the ledger and registry, Redis caching

## Status
Accepted

## Context
Two gaps were already flagged as blocking for production:

- ADR-0003: the audit ledger's signing key and `previous_hash` chain state live in
  a single process's memory — lost on restart, not shared across replicas, and not
  queryable as compliance evidence.
- ADR-0008: the Agent Registry is a git-reviewed YAML file. That is a good default
  (auditable, no infra dependency) but cannot support revoking an agent without a
  deploy, and 2026 non-human-identity guidance (Cloud Security Alliance NHI
  whitepaper; GitGuardian's NHI IAM strategy writeups) is consistent that agents
  need to be *governed assets* — inventoried, queryable, and instantly revocable —
  not config-file entries.

Both problems have the same shape: durable, queryable, multi-replica-safe state,
reachable from wherever the enterprise's own Postgres instance is.

## Decision

**Both the ledger and the registry get a Postgres-backed adapter**, selected by
`Settings.database_url`. When unset, the existing in-process ledger and
file-backed registry remain the zero-dependency dev/CI default — nothing about the
current test suite or `make up` flow requires Postgres.

- **Schema and migrations: SQLAlchemy (async) + Alembic.** Chosen over raw
  asyncpg/hand-written SQL because "if the user provides their own connection URL,
  we set it up on their instance" is exactly Alembic's job — `alembic upgrade head`
  against any Postgres the enterprise points us at, with a versioned migration
  history instead of a bespoke bootstrap script.
- **Ledger chain safety across replicas**: a single `chain_state` row holds
  `previous_hash`; `PostgresLedger.record()` takes a row lock
  (`SELECT ... FOR UPDATE`) on it inside the same transaction as the receipt
  insert, so concurrent writers across replicas serialize correctly instead of
  racing on the in-memory `asyncio.Lock` from ADR-0003.
- **Registry stays read-only from the gateway's perspective** in this ADR — no
  admin API to suspend/kill an agent yet, that is separate scope (an operator
  surface, not a data-layer concern). What lands here is the *capability* an
  instant kill switch would need: agent status is a queryable database row rather
  than a line in a file, and Redis caching (below) bounds how stale a read of that
  row can be.
- **Signing logic is extracted** into `adapters/ledger/signing.py` (canonical JSON
  + Ed25519 sign/verify) so the in-memory and Postgres ledgers share one
  implementation instead of duplicating crypto code.
- **Timestamps**: `RegisteredAgent` gains `created_at`, `updated_at`, and
  `status_changed_at` — the latter is what lets a future kill-switch feature (and
  compliance reporting) answer "when was this agent suspended," not just "is it
  suspended now."

## Redis caching

Two things are cached, both behind `Settings.redis_url` (unset = caching
disabled, adapters called directly):

- **Registry lookups** — every governed tool call does a registry read; caching it
  removes that read from the hot path. TTL defaults to 30s
  (`registry_cache_ttl_seconds`) — short enough that a status change (e.g. a future
  kill switch) propagates in seconds, not minutes, which is the deliberate
  trade-off for not having invalidation-on-write yet.
- **Brokered tokens** (RFC 8693 / Entra OBO results) — avoids an IdP round trip on
  every delegated call. TTL is derived from the token's own `exp` claim (peeked via
  an unverified JWT decode — we are not re-validating the IdP's signature, only
  reading a cache-lifetime hint from a response we already received over TLS from
  a trusted IdP) minus a safety margin (`token_cache_safety_margin_seconds`,
  default 30s), capped at `token_cache_max_ttl_seconds` (default 300s). Never
  cached past its real expiry. Opaque (non-JWT) tokens fall back to a short fixed
  TTL.

Both caches are implemented as **decorators over the existing ports**
(`CachingAgentRegistry` wraps any `AgentRegistry`; `CachingTokenExchange` wraps any
`TokenExchange`) — Redis is not a new port, it is an optional layer in front of an
existing one, so it composes with either the file or Postgres registry unchanged.

## Tenancy

Single-tenant per deployment, matching ADR-0007's stance that there is no
established cross-organization trust model yet: no `tenant_id` column or cache-key
namespace is introduced. An enterprise runs its own gateway against its own
Postgres and Redis. Multi-tenant is a larger change (needs a tenant claim
established upstream of the gateway) and is deliberately out of scope here.

## Consequences
- New optional dependencies: `sqlalchemy[asyncio]`, `asyncpg`, `alembic`,
  `redis`. All are in the main dependency group (not `--with examples`-style
  optional) since the adapters they back are selected at runtime by settings, but
  none of them are exercised unless `database_url`/`redis_url` are set.
- Cached reads mean **eventual, not immediate, consistency** for registry status
  and, to a much smaller degree, brokered tokens (bounded by their real
  `exp`). This is an explicit, documented trade-off, not an oversight — revisit if
  an admin kill-switch feature needs sub-TTL revocation (would need
  invalidate-on-write, e.g. a Redis pub/sub bust or moving off caching for status
  specifically).
- The Postgres ledger's `SELECT ... FOR UPDATE` on a single `chain_state` row is a
  deliberate serialization point — correct and simple, but it caps write
  throughput to one receipt at a time per chain. Acceptable for v1; a
  higher-throughput design (per-shard chains) is future work if it becomes a
  bottleneck.
