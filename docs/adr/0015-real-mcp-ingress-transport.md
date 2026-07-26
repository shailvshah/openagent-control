# ADR-0015: Real MCP ingress transport, mirroring ADR-0011's outgoing fix

## Status
Accepted

## Context
ADR-0011 fixed the gateway's *outgoing* MCP transport: the old upstream
adapter POSTed a bare JSON-RPC body, which any real MCP server rejects
(`406`, then `400 Missing session ID`) because Streamable HTTP requires an
`initialize` handshake, a negotiated protocol version, an `Mcp-Session-Id`
carried across requests, and SSE framing. The fix,
`StreamableHttpMCPUpstream`, delegates the entire transport to the official
MCP Python SDK.

That fix was never applied to the *incoming* side. `POST /mcp/v1`
(`gateway/routes/mcp.py`) still does exactly what the old outgoing adapter
did: parses a raw JSON body, no handshake, no session, no SSE. An internal
caller that already knows to speak plain JSON-RPC over HTTPS works today
(`examples/enterprise_scenario/`, the control-plane e2e test). A genuine
MCP-native client — the SDK's own client, LangChain's MCP adapter, Claude,
etc. — would be rejected the same way GitHub's production MCP server used to
reject this project's old outgoing adapter, for the identical reason.

Confirmed by direct inspection of the SDK before writing any code (not
assumed): `mcp.server.lowlevel.Server` plus
`mcp.server.streamable_http_manager.StreamableHTTPSessionManager` is the
SDK's own reference pattern for exposing a server over Streamable HTTP, and
`StreamableHTTPSessionManager.handle_request(scope, receive, send)` is a
plain ASGI callable — mountable directly into the existing FastAPI app.

## Decision

### Delegate to the SDK, same as ADR-0011
`gateway/mcp_server.py`'s `build_mcp_asgi_app(container)` constructs one
`mcp.server.lowlevel.Server`, registers `list_tools`/`call_tool` handlers
that translate to/from `GovernedExecutionService.execute(headers, payload)`
— the exact same call `gateway/routes/mcp.py` makes — and wraps the result in
`StreamableHTTPSessionManager`. `GovernedExecutionService` itself does not
change; this is purely a new transport adapter in front of it.
`create_app()` mounts it at `/mcp`, registered *after* the existing
`/mcp/v1` router so Starlette's first-match-wins resolution never lets the
mount intercept that path — `/mcp/v1` (raw JSON-RPC, not real MCP transport)
is kept, unchanged, for callers that don't need real MCP semantics, exactly
the treatment ADR-0011 gave `raw-jsonrpc` on the outgoing side.

### Real per-call HTTP headers reach the existing identity adapters unchanged
Traced through the SDK's source, not assumed: `Server.request_context` (a
public property) returns the SDK's `RequestContext`, whose `.request` field
is the actual Starlette `Request` for that specific call —
`StreamableHTTPServerTransport._create_session_message` attaches it via
`ServerMessageMetadata(request_context=request)`, which
`Server._handle_request` sets into a contextvar per request. A real MCP
client already sends `Authorization: Bearer <token>` per the MCP
authorization spec, so `identity_mode=oidc-jwks`/`jwt-svid` work against a
real MCP client with zero code changes. Custom headers this project invented
for delegated calls (`X-Human-Sponsor`, `X-Subject-Token`) remain a
pre-existing, orthogonal limitation — a stock MCP client won't send those
either way, on either transport; not a regression this work introduces.

### Error translation, verified against the real SDK client — not assumed
Two error paths, and they behave differently by design *and* by tested SDK
behavior:
- **`list_tools`**: any error (identity failure, registry-gate denial, policy
  denial) raises `McpError`. There is no per-item error shape for a listing,
  so any failure here is a protocol-level error. Verified: a real client's
  `list_tools()` call raises a real `McpError` it can catch.
- **`call_tool`**: every error case — policy DENY, identity failure,
  credential-broker failure, upstream error — becomes an in-band
  `CallToolResult(isError=True, content=[...])`, never a raised exception.
  This was a **planned distinction that testing disproved and corrected**:
  the original design raised `McpError` for identity/delegation failures
  specifically, reasoning that "you can't talk to this server at all" is a
  session-level concern. Actually running a real client against a real
  gateway showed `Server.call_tool()`'s own wrapper catches *any* exception
  raised from inside the registered handler — including `McpError` — and
  converts it to `CallToolResult(isError=True)` regardless. Raising a
  protocol-level error from inside `call_tool` is therefore dead code, never
  observable by a real client; the code was simplified to reflect what
  actually happens instead of what was assumed.

### Stateless mode
`StreamableHTTPSessionManager(app=server, stateless=True)`, not the SDK's
default session-tracking mode. This gateway already runs multi-replica
(ADR-0009, Postgres-backed ledger/registry); session-tracking mode would
silently introduce a session-affinity requirement (sticky LB routing) this
project has never needed anywhere else. One session per tool call was
already accepted on the *outgoing* side for the same correctness-first
reasoning (ADR-0011); stateless mode is the ingress-side equivalent
trade-off.

## Consequences
- The gateway now interoperates with real MCP clients on the incoming side,
  closing the mirror-image gap ADR-0011 left open on the outgoing side.
  Verified, not asserted:
  `tests/integration/test_mcp_ingress_streamable_http.py` reuses the full
  real stack from `test_enterprise_scenario.py` (real authorization server,
  real OPA, real downstream MCP server, real gateway under uvicorn) and
  drives it with the **real MCP SDK client** — the same reference client
  `StreamableHttpMCPUpstream` already trusts for the outgoing direction —
  proving a real handshake, a real brokered credential reaching a real
  downstream tool, an in-band policy denial, and a protocol-level identity
  failure, all against genuine components.
- A regression test confirms `/mcp/v1` still works unchanged after the `/mcp`
  mount exists — registration order is what keeps them from colliding, and
  that order is now load-bearing.
- **A known, tested limitation**: `Server.call_tool()`'s wrapper calls the
  registered `list_tools` handler internally, to cache tool definitions for
  input-schema validation, *before* a tool has ever been explicitly listed.
  If that internal probe itself hits an error (e.g. an orphaned or suspended
  agent, where the registry gate denies regardless of method), the resulting
  `CallToolResult` comes from the SDK's own generic exception handler rather
  than this module's explicit error-formatting branch — which means the
  `data.instruction` field governed_execution attaches to every denial is
  lost on a tool's *first* call in a session, though the denial reason itself
  still comes through. Explicit policy denials (not registry-gate ones) never
  hit this path, because the real Rego policy allows `tools/list`
  unconditionally.
- Real MCP clients over WebSocket are still not supported and are not
  planned: WebSocket isn't part of the official MCP spec, which defines only
  `stdio` and `Streamable HTTP` (SSE is Streamable HTTP's own
  server-to-client streaming mechanism, not a separate transport to add).
