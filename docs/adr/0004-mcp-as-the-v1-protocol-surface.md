# ADR-0004: MCP as the v1 governed protocol surface

## Status
Accepted

## Context
Agents reach tools through different transports: direct REST/SDK calls, and
increasingly the Model Context Protocol (MCP), which standardizes `tools/list` and
`tools/call` JSON-RPC methods across many vendors (including systems like enterprise
MCP server). Governing MCP gives one integration point for many downstream tools;
governing arbitrary REST calls means bespoke handling per API.

MCP calls carry an identity/authorization concern that is orthogonal to the transport
itself: OAuth 2.0. Two OAuth flows matter here — the On-Behalf-Of / token-exchange
flow (RFC 8693), used when an agent must inherit a strict subset of a human sponsor's
permissions before its `tools/call` reaches the target MCP server; and, per the MCP
authorization spec, the resource-server-style flow where the MCP server itself expects
a bearer token it can validate. The gateway sits in the middle of both: it terminates
the agent's inbound identity, drives the token exchange, and attaches the resulting
token to the outbound MCP call.

## Decision
v1 governs MCP traffic specifically: the gateway intercepts `tools/list` (to filter
which tools an agent is even told about) and `tools/call` (to validate arguments
before execution). OAuth 2.0 is the credential mechanism for that interception, not an
optional add-on: a `tools/call` that requires delegated (on-behalf-of) access must
carry a human subject token, which the gateway exchanges (RFC 8693) for a short-lived,
audience-scoped OBO token before forwarding to the target MCP server. Direct REST/SDK
interception (Pattern A/C from [ADR-0001](0001-hybrid-interception-pattern.md)) and
native LLM function-calling interception are later work.

## Consequences
- v1 cannot govern an agent that bypasses MCP and calls a target API directly with its
  own credentials — that gap must be closed by the target system's own access control,
  or by Pattern A (sidecar) later, not by this gateway alone.
- Policy input shape (`method`, `params.name`, `params.arguments`, `spiffe_id`, and —
  when present — the bound human subject token) is MCP-and-OAuth-shaped by design; a
  REST adapter added later needs its own translation into the same `PolicyEngine` port
  rather than a different policy input schema.
- The gateway depends on an external OAuth 2.0 authorization server (the enterprise
  IdP) supporting RFC 8693 token exchange. v1 will stub this dependency behind a
  `TokenExchange` port so the real IdP integration can land without touching the
  gateway or policy code — see [ADR-0006](0006-hexagonal-architecture-for-the-control-plane.md).
