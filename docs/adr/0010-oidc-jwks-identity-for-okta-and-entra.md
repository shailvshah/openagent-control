# ADR-0010: OIDC/JWKS identity validation for Okta and Microsoft Entra ID

## Status
Accepted

## Context
The Identity Plane so far authenticates agents two ways: a trusted header
(dev-only, ADR-0005) or a SPIFFE JWT-SVID (the SPIRE-issued shape). Neither
lets an agent authenticate with a token actually issued by an enterprise IdP
the firm already runs — Okta or Microsoft Entra ID — which is how most
non-SPIFFE workloads (service principals, OAuth client-credentials clients)
already prove their identity in these environments.

Both providers publish an OIDC discovery document and a JWKS; both rotate
signing keys and expect callers to cache and refresh rather than hardcode a
key (see the `enterprise-idp-integration` skill, built from each provider's
current docs, for the exact endpoint shapes and claim semantics this ADR
relies on).

## Decision
Add `OidcJwksIdentityProvider`, a single generic adapter driven entirely by
configuration (`oidc_discovery_url`, `oidc_audience`, optional `oidc_issuer`
override) rather than one adapter per vendor. This works because Okta and
Entra both speak standard OIDC discovery + JWKS; the parts that differ between
them (token-exchange vs. OBO grant shapes) live in the separate `TokenExchange`
port/adapters (ADR-0004), not in identity validation.

Implementation choices:
- **`PyJWT`'s `PyJWKClient`** for JWKS fetch/cache/rotation-by-`kid` — this is
  the caching behavior both providers document (refresh on an unrecognized
  `kid`, not a fixed poll), and it avoids hand-rolling a TTL cache.
- **The discovery document is fetched once at startup**, not per-request or
  lazily. Config errors (unreachable IdP, malformed document) become a startup
  failure instead of a per-request one — the same posture ADR-0005 established
  for the JWT-SVID trust-bundle key.
- **JWKS lookups run in a worker thread** (`asyncio.to_thread`) because
  `PyJWKClient` performs a blocking HTTP call on a cache miss; doing that
  inline would block the event loop for every concurrent request during a key
  rotation window.
- **Workload identity is derived from the client/app-id claim, not `sub`.**
  Machine (client-credentials) tokens from both providers identify the calling
  application via `azp`/`appid` (Entra) or `cid` (Okta) — `sub` on those tokens
  is often absent or equal to the client ID. Delegated (user) tokens carry a
  distinct `sub`; when present and different from the client id, it's surfaced
  as `AgentIdentity.human_sponsor`, not folded into the workload identity
  string. The resulting identity is `oidc://{issuer}/{client_id}`.

## Consequences
- Only one adapter to maintain for both providers; a third OIDC-compliant IdP
  needs no new code, only configuration.
- The `aud`/`iss` validation rules for **tenant-independent Entra endpoints**
  (`common`/`organizations`) are more involved than a single string compare —
  see references/entra.md in the skill. v1 is scoped to explicit
  `oidc_issuer`/tenant-specific discovery URLs; tenant-independent multi-tenant
  validation (matching the signing key's own issuer scope, not just the
  token's `iss`) is not implemented and should be treated as a gap if a
  multi-tenant Entra app is configured against this adapter.
- This is additive to identity modes, not a replacement: `header` and
  `jwt-svid` are unchanged; `identity_mode` selects exactly one at a time
  (ADR-0006 wiring).
