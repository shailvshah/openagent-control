# Governed Google ADK agent — day-1 demo

A Google Agent Development Kit (`google-adk`) `Agent` whose every tool call
routes through the openagent-control gateway: identity-attested,
OPA-policy-checked, and recorded as a signed, hash-chained audit receipt.

> Same integration surface as [`examples/langgraph_governed_agent/`](../langgraph_governed_agent/README.md),
> for Google ADK instead of LangChain/LangGraph — `@governed` is a plain
> Python decorator with no framework import in it, so the same governance
> mechanism works for either. See the `google-adk` skill for the general
> framework reference this example draws on.

## What it shows

1. `read_query` → **ALLOWED** by `policies/mcp_authz.rego` for
   `spiffe://corp.net/ns/finance/agent/invoice-bot`.
2. `salesforce_update_account` → **DENIED** (capability not granted to this
   identity, per `registry/agents.yaml`). The gateway returns a semantic error
   payload; the model reads *"Stop execution and request user approval"* and
   asks for approval instead of retrying.
3. Both decisions appear in the gateway log as Ed25519-signed receipts, each
   chained to the previous receipt's hash.

## A separate venv, on purpose

`google-adk`'s own dependencies are real and current (`httpx>=0.28.1`,
`fastapi>=0.133`, `cryptography>=44`) — well outside this repo's pinned
versions (pinned for the gateway's own test matrix, not because the SDK
client needs exactly that range). A plain `pip install google-adk
openagent-control` in one venv will report hard version conflicts. Verified
working instead: install ADK first, then layer `openagent-control` with
`--no-deps` — the SDK client's actual runtime need is just `httpx` +
`pydantic`, both already present, newer, from ADK's own install.

```bash
python3 -m venv .venv-adk && source .venv-adk/bin/activate
pip install "google-adk>=2.5.0" litellm   # litellm only needed for the live-model path below
pip install --no-deps openagent-control   # or: --no-deps -e /path/to/this/repo, from a checkout
```

## Run it

```bash
make up-demo   # from the main repo/venv: gateway :8000, OPA :8181
```

Then, in the ADK venv:

```bash
poetry run python -m examples.google_adk_governed_agent.demo   # or: python -m examples...
```

By default this calls the governed tools **directly** (no LLM involved) —
deterministic, zero API keys, and it still proves the real ALLOW/DENY
decisions and receipts, since the tool calls go through the real gateway
either way.

### Run it against a real model

```bash
export ANTHROPIC_API_KEY=sk-ant-...
poetry run python -m examples.google_adk_governed_agent.demo
```

Verified end-to-end with the default, `anthropic/claude-haiku-4-5-20251001`,
via ADK's own `LiteLlm` wrapper (ADK's *native* models are Gemini; `LiteLlm`
is ADK's documented path to any other provider — not a workaround this
example invented). A real model turn: both tools called (in parallel), a real
ALLOW, a real DENY, both receipted, and the model asks for approval instead
of retrying. Override with `ADK_MODEL`.

## Files

- `demo.py` — builds the `Agent` + `InMemoryRunner` and runs the turn
- `governed_tools.py` — the tool factory: ADK needs no tool decorator at all
  (plain functions go straight into `Agent(tools=[...])`), so `@governed`
  (framework-agnostic) sits directly on each function — see
  [ADR-0017](../../docs/adr/0017-client-sdk-and-authorize-only-endpoint.md)
