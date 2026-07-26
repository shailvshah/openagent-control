# ADR-0011: Speak MCP via the official SDK, not hand-rolled JSON-RPC

## Status
Accepted

## Context
The gateway is described as an **MCP gateway**. Until now its upstream adapter,
`HttpMCPUpstream`, POSTed a bare JSON-RPC body to a URL:

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {...}}
```

That is not the MCP transport. Pointed at a genuine MCP server it fails twice
over:

```
HTTP 406  Not Acceptable: Client must accept both application/json and text/event-stream
HTTP 400  Bad Request: Missing session ID
```

Streamable HTTP requires an `initialize` handshake, a negotiated protocol
version, an `Mcp-Session-Id` carried across requests, SSE framing, and a
`DELETE` to terminate the session.

This went unnoticed because the repo's own example MCP server was written to
accept exactly what the adapter sent. Both sides were wrong in the same
direction, so every test passed — the same class of blind spot ADR-0010 records
for identity, where our own authorization server shared the adapter's mistaken
assumption about `sub`. **A test suite cannot detect a protocol error that both
its client and its server make.**

Verified against two real servers: a server built with the official MCP Python
SDK, and GitHub's production MCP server at `https://api.githubcopilot.com/mcp/`.

## Decision
Add `StreamableHttpMCPUpstream`, which delegates the entire transport to the
**official MCP Python SDK** (`mcp.ClientSession` +
`mcp.client.streamable_http`), and make it the default `mcp_upstream_mode`.

The protocol is a moving target with a versioned spec, session semantics, and
SSE framing. Re-implementing it here would mean re-deriving the reference
implementation and re-acquiring its bug fixes; the SDK is maintained alongside
the specification. `mcp` is therefore a core dependency, not an extra — an MCP
gateway that cannot speak MCP has no reason to exist.

`HttpMCPUpstream` is retained behind `mcp_upstream_mode="raw-jsonrpc"` and
documented honestly as plain JSON-RPC over HTTP, suitable only for an internal
endpoint that is *not* an MCP server.

Supporting choices:
- **Errors are unwrapped before being reported.** The SDK runs sessions in an
  anyio task group, so failures arrive as `ExceptionGroup: unhandled errors in
  a TaskGroup (1 sub-exception)`. That string is what would otherwise reach the
  agent in the denial payload, where the entire design intent (ADR-0004) is
  that an LLM receives an actionable instruction rather than a raw stack error.
  `_describe` flattens the group to the real causes.
- **One MCP session per tool call.** Costs an extra initialize round trip
  versus a pooled session. Accepted for v1: a pooled session is stateful, must
  be rebuilt when the upstream restarts, and needs concurrency control of its
  own. Correctness first; pooling is a measurable optimisation later.

## Consequences
- The gateway now interoperates with real MCP servers, including GitHub's.
  This is verified, not asserted: `tests/unit/test_mcp_upstream_streamable.py`
  runs against a real SDK-built server on every `make check`.
- The example MCP server was rewritten onto `FastMCP` with a `TokenVerifier`,
  so the scenario exercises the real protocol *and* gets RFC 9728
  protected-resource metadata plus the `WWW-Authenticate` 401 challenge from
  the SDK — both required by the MCP authorization spec and neither previously
  implemented.
- **Token passthrough is forbidden by the MCP spec**, and the gateway already
  complies by construction: it brokers a new token scoped to the upstream's own
  audience (ADR-0004) rather than relaying the agent's. The audience check on
  the resource server is what makes bypassing the gateway impossible.
- Per-call session setup adds latency to every governed tool call. Not yet
  measured under load; flagged in docs/roadmap.md rather than claimed as
  negligible.
- `uvicorn` had to be unpinned from `<0.31.0` to satisfy the SDK.
- Tool results are now MCP `CallToolResult` objects (`content` blocks plus
  `structuredContent`), not arbitrary upstream JSON. Callers read
  `result.structuredContent`.
