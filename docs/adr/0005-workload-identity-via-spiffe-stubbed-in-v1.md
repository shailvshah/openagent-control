# ADR-0005: Workload identity via SPIFFE, stubbed in v1

## Status
Accepted

## Context
Agents need a verifiable identity independent of the human operating them, so policy
and audit can be keyed on "which agent" rather than "which shared credential." SPIFFE
(and SPIRE as the runtime that issues SVIDs) is the emerging standard for workload
identity and is the identity primitive referenced by IETF's draft AIMS work on agent
identity.

Running a real SPIRE server/agent is infrastructure the foundation phase doesn't need
yet to prove out the domain logic (policy evaluation, credential brokering, audit
chaining).

## Decision
Define an `IdentityProvider` port now. Ship one adapter in v1 that reads a
`X-Spiffe-ID` header (i.e., trusts an already-attested caller, such as a service mesh
sidecar) rather than performing SPIRE attestation itself. This is explicitly a
development/integration stub, not a security boundary.

## Consequences
- v1 does **not** cryptographically verify agent identity end-to-end. Anyone who can
  reach the gateway can claim any SPIFFE ID via the header. This is acceptable only
  behind a trusted network boundary (e.g. inside a mesh that itself does mTLS/SPIFFE
  attestation) and must be called out any time v1 is described as "production-ready."
- A real SPIRE-backed adapter (Unix domain socket Workload API) is a drop-in
  replacement behind the same port — tracked as follow-up work, not blocking the
  foundation.
