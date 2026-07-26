# ADR-0006: Hexagonal (ports & adapters) architecture for the control plane

## Status
Accepted

## Context
Every core decision in this project so far (OPA vs. Cedar, SPIFFE stub vs. real SPIRE,
in-process signing key vs. KMS, OAuth token exchange stub vs. real IdP) is something
we expect to swap out as the project matures from foundation to production. If the
FastAPI route handlers call OPA's HTTP API, read headers, and sign receipts directly,
every one of those swaps becomes a scattered rewrite.

We also already know the shape of the first real-world integrations, across four
categories:
- **Identity providers** — Okta, Microsoft Entra ID (the root of trust for human
  sponsors and OAuth token exchange).
- **Target/tool surfaces** — MCP servers, Slack, and, later, plain OpenAPI-described
  REST APIs.
- **Observability/SIEM sinks** — Grafana and Datadog-shaped destinations for metrics
  and signed audit receipts.
- **Agent frameworks** — LangChain/LangGraph and CrewAI, which call tools through their
  own node/task abstractions rather than raw HTTP.

None of these should be hardcoded into the domain core.

## Decision
Structure the codebase as domain core + ports + adapters:
- `domain/` — pure models (`AgentIdentity`, `ToolCallRequest`, `PolicyDecision`,
  `ExecutionReceipt`) and `Protocol`-typed ports (`PolicyEngine`, `IdentityProvider`,
  `Ledger`, `TokenExchange`, `CredentialBroker`). No I/O, no framework imports.
- `adapters/` — concrete implementations of each port. Each adapter only knows its own
  external dependency, never the FastAPI layer.
- `gateway/` — the FastAPI app: routes, dependency injection wiring adapters into
  ports, request/response shaping. No policy or crypto logic lives here.

Declare integration-support ports now, one per category above, even though v1 ships
only minimal or stub adapters behind most of them:

| Port | Category | v1 adapter | Later adapters |
|---|---|---|---|
| `TokenExchange` | Identity provider | stub (mocked RFC 8693 response) | Okta, Microsoft Entra ID |
| `MCPUpstream` | Target/tool surface | forwards to a mock MCP server | MCP servers |
| `ToolUpstream` | Target/tool surface | not implemented in v1 | OpenAPI-described REST APIs |
| `AuditExporter` | Observability/SIEM | stdout/log adapter | Datadog, Grafana (via OTLP or their APIs) |
| `MetricsSink` | Observability/SIEM | OpenTelemetry no-op/console exporter | Grafana/Datadog metrics backends |
| `ApprovalChannel` | HITL (cross-cutting) | not implemented in v1 | Slack, Jira |
| — agent-framework integration — | Agent frameworks | none in v1 (governed via the MCP/egress gateway pattern, framework-agnostic) | LangChain/LangGraph node wrapper, CrewAI tool wrapper — these are Pattern C (native SDK) from [ADR-0001](0001-hybrid-interception-pattern.md), calling the same ports above rather than duplicating them |

## Consequences
- More files and indirection than a single `main.py` for the same v1 feature set —
  accepted deliberately, because the project's near-term roadmap (Cedar, real SPIRE,
  Vault, RFC 8693 token exchange, Okta/Entra, Grafana/Datadog, LangGraph/
  CrewAI, OpenAPI targets) is exactly the kind of change this structure is meant to
  absorb without touching the gateway.
- Every new adapter must be tested against its port's contract (not just its own
  happy path) so swapping implementations doesn't silently change behavior at the
  gateway.
- Declaring these ports now — even with stub or missing v1 implementations — means
  each future integration is additive (a new adapter file plus a table row moving from
  "not implemented" to "implemented"), not a change to the domain core or gateway
  wiring.
- This is the "foundation" referred to in the design doc: it constrains where phase 2
  work (function-calling interception, HITL, DID/VC federation) plugs in, without
  dictating how those features work internally.
