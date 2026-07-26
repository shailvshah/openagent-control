# ADR-0017: A client SDK, and the authorize-only endpoint it needs

## Status
Accepted

## Context
`pip install openagent-control` shipped a gateway and no client. The only
client-side code in the repo was `examples/langgraph_governed_agent/governed_tools.py`,
which the wheel does not contain, so adopting this project meant copying an
example and hand-writing JSON-RPC over httpx. Every adopter would write the
same envelope, the same error unwrapping, and the same header handling, each
slightly differently.

The deeper problem was that only one integration shape existed at all.
Everything went through the proxy: the agent's call travelled to the gateway
and on to an upstream MCP server. That suits an agent whose tools already live
behind MCP. It does not suit the agent this project most wants to reach — one
already running in production, whose tool functions are ordinary Python in its
own process, calling Salesforce or a database directly. Governing those meant
moving them behind an MCP server first: a rewrite nobody schedules, to adopt a
control plane they have not yet decided to trust.

ADR-0001 named this second pattern ("native SDK") as part of the intended
hybrid and left it unbuilt. The gateway had no endpoint for it either — every
route forwards on ALLOW, and there was no way to ask "may I?" without also
asking the gateway to run something.

## Decision

### `POST /api/v1/authorize` — decide and receipt, do not execute
`GovernedExecutionService.authorize()` is `execute()` minus credential
brokering and the upstream forward. It was extracted as the shared first half
rather than written as a second path, so `execute()` now calls it: there is one
implementation of identify → registry gate → policy → receipt → export, and no
way for the two entry points to drift on a security decision.

The response is deliberately not a JSON-RPC envelope. Dressing a decision up as
a tool-call result would imply something ran. It carries the decision, the
reason, the gateway's own agent-readable instruction, the receipt id, and
`shadowed` — so a caller can tell "allowed" from "would have been blocked, but
this gateway is in observe mode" (ADR-0012).

### `openagent_control.sdk`
Ships in the wheel, depends only on httpx, which the gateway already requires.

- `GovernedClient` / `AsyncGovernedClient` — sync and async as separate classes,
  not one class with a flag. An agent runtime is usually async and a script
  usually is not, and a sync method that secretly blocks an event loop is a bug
  that only appears under load.
- `@governed(client)` — the decorator. Asks for a decision, then runs the
  function, or raises `ToolCallDenied` and does not.
- `client.call_tool()` / `client.list_tools()` — the proxy shape, for tools that
  really are on an MCP server, without hand-writing JSON-RPC.

Three decisions inside the decorator that are not obvious:

**Arguments are bound through the signature, not forwarded as `**kwargs`.**
Otherwise `update_account("ACC-1", 50000)` sends no `credit_limit` to policy
while `update_account(credit_limit=50000)` does — a guardrail bypass reachable
by changing nothing but call style. `apply_defaults()` is applied too, so a
threshold rule sees the value that will actually be used.

**Denial behaviour is a choice, because the right answer differs by caller.**
In plain Python, raising is correct: the function did not run, and a return
value would be a lie. Inside an agent framework it is wrong — an exception
propagates out of the tool node and ends the run, when what you want is the
model reading the denial and stopping. `on_deny="raise"` is the default;
`on_deny="return"` returns the gateway's instruction as the tool's output, and
the LangChain integration uses it.

**Wrapping anything that is not a plain function is refused, with the fix in
the message.** `@governed` must be the inner decorator; reversed, it would wrap
a framework's tool *object*, `functools.wraps` would copy the wrong metadata,
and the failure would surface far from its cause.

### `openagent_control.sdk.langchain`
`langchain` is imported inside the functions, never at module scope: it is not a
dependency of this project, and importing the SDK must not require it. Pinning
a version here would put this project in the middle of an agent framework's
release cadence for no benefit.

`proxied_tools()` builds each tool's argument schema from the MCP tool's own
advertised `inputSchema`. This was **found by testing, not designed**: the first
version returned a bare `**kwargs` proxy, LangChain had nothing to introspect,
and it invoked the tool with no arguments at all — the upstream rejected the
call for a missing required field, and the tool looked broken when the real
cause was a schema never handed over.

## Consequences
- Adoption no longer requires moving tool code. An agent in production adds a
  client, a decorator, and a registry entry.
- Verified against a fully real stack
  (`tests/integration/test_sdk_end_to_end.py`): real authorization server, real
  `opa` evaluating the real Rego, real downstream MCP server, real gateway
  under uvicorn on a real socket. The load-bearing test is
  `test_governed_blocks_a_local_function_when_real_policy_denies` — real OPA
  evaluated the real policy against the real registry, and the local Python
  side effect did not happen.
- Verified against **real LangChain and a real compiled LangGraph graph**, not
  a stand-in tool decorator, because the entire risk is how a real framework
  introspects a decorated function. That is what caught the `inputSchema`
  defect above, and it also established that `@governed` is transparent to
  LangChain's name/schema/description inference.
- `authorize()` gates on the decision but does **not** broker a credential, by
  design: the agent runs the tool with whatever credential it already holds.
  This pattern therefore delivers identity, policy and audit — not the
  credential-scoping property that makes gateway bypass impossible. An agent
  can ignore the SDK and call Salesforce directly. The proxy shape remains the
  stronger guarantee, and that difference belongs in the deployment guidance
  rather than being papered over: the SDK is the on-ramp, not the end state.
- One SDK-shaped gap remains: `list_tools()` and `call_tool()` speak to
  `/mcp/v1` (raw JSON-RPC), not the real MCP transport at `/mcp`. Both are
  governed identically (ADR-0015); the raw path is simply a smaller client to
  maintain. An SDK user who wants the real transport should use the MCP SDK's
  own client against `/mcp`.
