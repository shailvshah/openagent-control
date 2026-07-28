# Governed CrewAI agent — day-1 demo

A CrewAI `Agent`/`Task`/`Crew` whose every tool call routes through the
openagent-control gateway: identity-attested, OPA-policy-checked, and recorded
as a signed, hash-chained audit receipt.

> Same integration surface as [`examples/langgraph_governed_agent/`](../langgraph_governed_agent/README.md),
> for CrewAI instead of LangChain/LangGraph — `@governed` is a plain Python
> decorator with no framework import in it, so the same governance mechanism
> works for either. See the `crewai` skill for the general framework
> reference this example draws on.

## What it shows

1. `read_query` → **ALLOWED** by `policies/mcp_authz.rego` for
   `spiffe://corp.net/ns/finance/agent/invoice-bot`.
2. `salesforce_update_account` → **DENIED** (capability not granted to this
   identity, per `registry/agents.yaml`). The gateway returns a semantic error
   payload; the agent reads *"Stop execution and request user approval"* and
   stops, instead of retrying.
3. Both decisions appear in the gateway log as Ed25519-signed receipts, each
   chained to the previous receipt's hash.

## A separate venv, on purpose

CrewAI's own dependencies (real ones — `httpx~=0.28.1`, etc.) fall outside
this repo's pinned versions (`httpx>=0.27.0,<0.28.0`, pinned for the gateway's
own test matrix, not because the SDK client needs exactly that range).
Verified: installing CrewAI's dependencies first, then layering
`openagent-control` with `--no-deps`, works cleanly — the SDK client's actual
runtime need is just `httpx` + `pydantic`, both already present from CrewAI's
own install.

```bash
python3 -m venv .venv-crewai && source .venv-crewai/bin/activate
pip install "crewai[anthropic]"          # [anthropic] only needed for the live-model path below
pip install --no-deps openagent-control  # or: --no-deps -e /path/to/this/repo, from a checkout
```

## Run it

```bash
make up-demo   # from the main repo/venv: gateway :8000, OPA :8181
```

Then, in the CrewAI venv:

```bash
poetry run python -m examples.crewai_governed_agent.demo   # or: python -m examples...
```

By default this calls the governed tools **directly** (no LLM involved) —
deterministic, zero API keys, and it still proves the real ALLOW/DENY
decisions and receipts, since the tool calls go through the real gateway
either way.

### Run it against a real model

```bash
export ANTHROPIC_API_KEY=sk-ant-...
poetry run python -m examples.crewai_governed_agent.demo
```

Verified end-to-end with the default, `anthropic/claude-haiku-4-5-20251001` —
a real Crew kickoff, the model deciding to call both tools, a real ALLOW and
a real DENY, both receipted. Override with `CREWAI_MODEL`. One real
compatibility quirk found running this: CrewAI's *native* Anthropic
completion path (not litellm) sends an assistant-message prefill that
`claude-sonnet-4-6` rejects outright (`"This model does not support assistant
message prefill"`) — a CrewAI/model interaction, not anything on the gateway
side. If you hit that switching models, that's why.

## Files

- `demo.py` — builds the Crew and runs the task
- `governed_tools.py` — the tool factory: `@governed` (framework-agnostic)
  wrapped by CrewAI's own `@tool`, `@governed` on the inside so CrewAI's
  schema introspection sees the original function signature through the
  wrapper — see [ADR-0017](../../docs/adr/0017-client-sdk-and-authorize-only-endpoint.md)
