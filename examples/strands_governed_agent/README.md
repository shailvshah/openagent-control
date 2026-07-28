# Governed Strands agent — day-1 demo

An AWS Strands Agents `Agent` whose every tool call routes through the
openagent-control gateway: identity-attested, OPA-policy-checked, and
recorded as a signed, hash-chained audit receipt.

> Same integration surface as [`examples/langgraph_governed_agent/`](../langgraph_governed_agent/README.md),
> for Strands instead of LangChain/LangGraph — `@governed` is a plain Python
> decorator with no framework import in it, so the same governance mechanism
> works for either. See the `strands-agents` skill for the general framework
> reference this example draws on.

## What it shows

1. `read_query` → **ALLOWED** by `policies/mcp_authz.rego` for
   `spiffe://corp.net/ns/finance/agent/invoice-bot`.
2. `salesforce_update_account` → **DENIED** (capability not granted to this
   identity, per `registry/agents.yaml`). The gateway returns a semantic error
   payload; the model reads *"Stop execution and request user approval"* and
   stops, instead of retrying.
3. Both decisions appear in the gateway log as Ed25519-signed receipts, each
   chained to the previous receipt's hash.

## A separate venv, on purpose

`strands-agents` depends on the official `mcp` SDK at a version that requires
`httpx>=0.28.1` — outside this repo's pinned versions (pinned for the
gateway's own test matrix, not because the SDK client needs exactly that
range). This isn't just a warning here: `pip install strands-agents
openagent-control` in one venv is **unresolvable** — pip refuses outright.
Verified working instead: install Strands first, then layer
`openagent-control` with `--no-deps` — the SDK client's actual runtime need
is just `httpx` + `pydantic`, both already present, newer, from Strands' own
install.

```bash
python3 -m venv .venv-strands && source .venv-strands/bin/activate
pip install strands-agents anthropic     # anthropic only needed for the live-model path below
pip install --no-deps openagent-control  # or: --no-deps -e /path/to/this/repo, from a checkout
```

## Run it

```bash
make up-demo   # from the main repo/venv: gateway :8000, OPA :8181
```

Then, in the Strands venv:

```bash
poetry run python -m examples.strands_governed_agent.demo   # or: python -m examples...
```

By default this calls the governed tools **directly** (no LLM involved) —
deterministic, zero API keys, and it still proves the real ALLOW/DENY
decisions and receipts, since the tool calls go through the real gateway
either way.

### Run it against a real model

```bash
export ANTHROPIC_API_KEY=sk-ant-...
poetry run python -m examples.strands_governed_agent.demo
```

Verified end-to-end with the default, `claude-haiku-4-5-20251001`, via
Strands' own `AnthropicModel`. A real agent turn: both tools called, a real
ALLOW, a real DENY, both receipted, and the model reports the block and asks
for approval instead of retrying. Override with `STRANDS_MODEL`.

## Files

- `demo.py` — builds the `Agent` and runs the turn
- `governed_tools.py` — the tool factory: `@governed` (framework-agnostic)
  wrapped by Strands' own `@tool`, `@governed` on the inside so Strands'
  schema introspection sees the original function signature through the
  wrapper — see [ADR-0017](../../docs/adr/0017-client-sdk-and-authorize-only-endpoint.md)
