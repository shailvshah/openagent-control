# OpenAgent-Control — System Design

## 1. Context & Objective

**Problem.** Autonomous AI agents today typically run on static service accounts and
hardcoded API keys. Because LLM-driven execution paths are non-deterministic, static
credentials force a choice between over-privileged agents and brittle, hand-wired
workflows. Standard API gateways don't understand *tool call* semantics, and
conventional application logs don't meet the non-repudiation bar regulators expect for
autonomous decisions.

**Objective.** Provide an open-source, enterprise-grade Agent Identity & Governance
Control Plane. Agents are first-class identities. Every tool invocation (direct API or
Model Context Protocol) is authorized against explicit policy, executed with a
short-lived, task-scoped credential, and recorded in a cryptographically verifiable,
tamper-evident ledger — without requiring the agent's own application code to be
rewritten.

Non-goals for v1: multi-cloud secret backends beyond Vault, a built-in semantic
guardrail model, and cross-organization (DID/VC) trust — see [ADR-0007](adr/0007-decentralized-identity-is-a-future-extension-not-v1-scope.md).

## 2. High-Level Architecture

OpenAgent-Control uses a **hybrid interception pattern**: it can run as a network
proxy (gateway or sidecar) or as an in-process middleware SDK. Either way, it sits
between an agent and the tools/systems it calls, and it never lets the agent hold a
long-lived credential to the target system.

```
Agent → [Identity Attestation] → [OpenAgent-Control] → [Policy Decision] → [Credential Broker] → Target System
                                          │
                                          └──→ [Audit Ledger] → SIEM
```

1. **Identity attestation** — the agent authenticates as a workload (SPIFFE SVID or
   equivalent), not as a shared secret.
2. **Interception** — the agent's tool call (MCP `tools/call`, or a direct API call) is
   routed through the control plane before it reaches the target.
3. **Policy evaluation** — request, agent identity, and any bound human-sponsor context
   are evaluated against policy (OPA/Rego for v1).
4. **Credential brokering** — on allow, a short-lived (minutes, not months) credential
   is minted or injected for that single call.
5. **Execution & audit** — the call executes; an Ed25519-signed, hash-chained receipt
   is generated asynchronously and streamed to the audit ledger / SIEM.

## 3. Core Components

| Component | Function | v1 Implementation |
|---|---|---|
| Identity Broker | Workload identity & token issuance; maps agent identity to an optional human sponsor | SPIFFE ID (stubbed via header initially, SPIRE integration later) |
| Policy Engine | Deterministic authorization over tool calls | Open Policy Agent (Rego) |
| MCP Gateway | Protocol-level governance: filters `tools/list`, validates `tools/call` arguments | FastAPI proxy |
| Audit Ledger | Compliance & non-repudiation | Ed25519-signed, hash-chained JSON receipts |
| Credential Broker | Converts a policy "allow" into a scoped, short-lived credential | Stub in v1; Vault dynamic secrets is the intended production backend |

See [ADR-0002](adr/0002-opa-rego-as-the-v1-policy-engine.md),
[ADR-0003](adr/0003-ed25519-hash-chained-audit-ledger.md), and
[ADR-0004](adr/0004-mcp-as-the-v1-protocol-surface.md).

## 4. Integration & Deployment Patterns

Three deployment models, in increasing order of integration effort:

- **Pattern A — Kubernetes sidecar** (zero code change): a mutating webhook injects an
  Envoy/gateway sidecar into pods labeled for governance; secures east-west traffic and
  local DB queries.
- **Pattern B — Egress MCP/LLM gateway** (zero code change): the agent's MCP or LLM
  client base URL points at the control plane; used for outbound calls to systems like
  Salesforce, Snowflake, DealCloud.
- **Pattern C — Native SDK / middleware** (low code change): a Python decorator wraps
  LangGraph/CrewAI tool nodes for stateful HITL interruption.

v1 targets **Pattern B** first — it has the best effort-to-value ratio and requires no
changes to target systems. See [ADR-0001](adr/0001-hybrid-interception-pattern.md).

## 5. Security & Audit Model

**Authorization models:**
- *Delegated access (On-Behalf-Of)* — the agent inherits a strict subset of the
  invoking human's permissions via OAuth 2.0 token exchange (RFC 8693).
- *Direct access (autonomous)* — the agent acts under its own workload identity and
  scope matrix, no human in the loop for that call.

**Cryptographic non-repudiation:** every policy decision is hashed and chained to the
previous decision's hash, then signed (Ed25519). A completed workflow can be "sealed,"
giving deterministic proof of what was authorized and executed — the basis for SOC 2,
ISO 42001, and EU AI Act Article 12 logging requirements.

## 6. Known Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Latency from synchronous hash-chaining | Signing is decoupled to an async path; live requests only block on the policy decision, not on ledger writes |
| LLM retry-looping on a bare HTTP 403 | Denials return a structured payload the agent can reason about (e.g. `{"status": "blocked", "instruction": "stop and request approval"}`) instead of a raw error |
| Undetected exfiltration via many small "allowed" calls | Deterministic per-call policy is paired with velocity/quota checks, not implemented in v1 |
| Fragmented human approval across systems | Out of scope for v1; noted for a future HITL module |

## 7. Standards Alignment (informational, re-verify before citing externally)

The shape of this design tracks publicly discussed 2026 direction from IETF (AIMS,
composing SPIFFE + WIMSE + OAuth for agent auth), OWASP's Agentic Top 10 (tool misuse,
delegation/privilege abuse), and NIST's push for continuous verification and
non-repudiable logging of autonomous action. Treat specific standard names/numbers as
pointers to go verify, not settled citations — they were not independently confirmed
before being written here.

## 8. What "Foundation" Means for v1

Per [ADR-0006](adr/0006-hexagonal-architecture-for-the-control-plane.md), the codebase
is structured so the three core decisions above (which policy engine, which identity
mechanism, which ledger backend) are swappable without touching the gateway or domain
logic: domain models and ports first, adapters second, wiring last. This is what lets a
Vault-backed credential broker or a Cedar policy engine be added later as a new
adapter, not a rewrite.
