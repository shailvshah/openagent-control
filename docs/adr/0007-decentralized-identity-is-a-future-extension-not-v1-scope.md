# ADR-0007: Decentralized identity (DIDs/VCs) is a future extension, not v1 scope

## Status
Accepted

## Context
The design conversation that produced this project also explored an "Internet 3.0"
future where agents from different organizations negotiate and transact without a
shared, centralized identity provider — using W3C Decentralized Identifiers (DIDs) and
Verifiable Credentials (VCs) instead of an internal SPIFFE ID and an OAuth token from a
known IdP.

That is a real and useful direction, but it solves a cross-organization trust problem
this project does not have yet: v1's entire identity model ([ADR-0005](0005-workload-identity-via-spiffe-stubbed-in-v1.md))
and OAuth flow ([ADR-0004](0004-mcp-as-the-v1-protocol-surface.md)) assume a single
enterprise's IdP is the root of trust.

## Decision
Do not build DID/VC support in the foundation. Keep the identity and policy ports
([ADR-0006](0006-hexagonal-architecture-for-the-control-plane.md)) generic enough that
a `did:...`-based `IdentityProvider` adapter and a VC-based `PolicyEngine` input could
be added later without redesigning the domain core — but do not build them now.

## Consequences
- Any inbound request from an agent outside the enterprise's own IdP/SPIFFE trust
  domain is out of scope for v1 and should be rejected, not partially handled.
- Revisit this decision once the enterprise (centralized) foundation is proven in
  production and there is a concrete cross-organization use case, not speculatively.
