# ADR-0003: Ed25519-signed, hash-chained audit receipts

## Status
Accepted

## Context
A policy decision (allow/deny) and the tool call it governs must be provable after the
fact — not just logged, but non-repudiable, so a compliance reviewer can trust the
record wasn't altered retroactively. Plain application logs (even centralized ones)
don't give that guarantee: anyone with write access to the log store can edit history.

## Decision
Every policy decision produces a receipt containing: a sequence id, timestamp, agent
identity, the decision and reason, a hash of the request payload, and the hash of the
*previous* receipt in the chain. The receipt is serialized to canonical JSON and signed
with Ed25519. Chaining makes any retroactive edit detectable (it breaks the hash
chain); signing makes the origin of each receipt verifiable.

Signing happens on an async path (background worker), not inline in the request/response
cycle, per the latency mitigation in the design doc — the agent's tool call is not held
up waiting for a signature.

## Consequences
- The signing key's custody matters: v1 generates an in-process key at startup, which
  is not production-safe (key is lost on restart, chain 're-roots'). Production needs
  the key sourced from a proper KMS/HSM — tracked as follow-up, not solved here.
- A single in-process `previous_hash` variable is a single point of chain state; a
  multi-replica gateway needs a shared, race-safe store for chain state before this
  scales horizontally — noted as a scaling gap, not solved in v1's foundation.
- "Sealing" a completed sequence (marking it closed for audit export) is a documented
  concept in the design doc but not implemented as a v1 API.
