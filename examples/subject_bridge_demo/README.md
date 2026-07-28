# Subject bridge demo — proof for ADR-0020

Proves the boundary-1/boundary-2 split [ADR-0020](../../docs/adr/0020-inbound-agent-serving-is-out-of-scope-the-boundary-is-subject-token.md)
describes, end to end, against real components: a real signed-JWT
authorization server, a real `opa` process, a real MCP server, the real
gateway, and a real HTTP request into `boundary1_app.py` — the code the ADR
says is the *only* wiring a deployer has to write.

## The scenario

One agent identity (`invoice-bot`, a stable, autonomous `client_credentials`
token — no per-user re-authentication). Two different humans call it through
the same endpoint:

- **`dana@corp.net`** — has the `finance-approver` role → **ALLOWED**
- **`intern@corp.net`** — does not → **DENIED**

Same agent, same registry grant (`update_record` is granted outright), same
tool call. The only thing that differs between the two requests is which
human's token `boundary1_app.py` read off the `Authorization` header and
threaded through as `subject_token=`. A real OPA process reads
`input.subject.roles` from a real, independently re-verified JWT to make that
call — not from anything the agent or boundary 1 merely asserted.

## A real bug this found

Building this exposed a genuine gap, not a demo issue: `GovernedExecutionService`
only ran subject verification when the *agent's own* token carried a
`human_sponsor` claim — i.e., it assumed the agent re-authenticates per user
(an OBO-style flow), not that a stable agent identity forwards a different
`subject_token` per request. That's exactly this scenario's shape, and it had
zero test coverage at this level (`tests/integration/test_subject_authorization.py`
calls `OPAPolicyEngine.evaluate()` directly, bypassing this gate entirely).
Fixed in `governed_execution.py`: subject verification now triggers on the
*subject token's presence*, not on the agent's own sponsor claim.
`_bind_subject` already treated an unset `human_sponsor` as "nothing to bind
against" rather than a mismatch, so the fix doesn't weaken the binding check
— see the code comment there for the full reasoning.

## Run it

```bash
brew install opa   # the policy engine is real
poetry run python -m examples.subject_bridge_demo.demo
```

No Docker, no external services — everything (IdP, OPA, MCP server, gateway,
the boundary-1 endpoint) runs in-process on real local ports, the same
pattern `examples/enterprise_scenario/` uses.

## Files

- `boundary1_app.py` — the agent's own serving endpoint: a tiny FastAPI app
  representing the code that runs *after* an API gateway/ASGI middleware has
  already validated the caller's OIDC token. Reads `Authorization`, threads
  it through as `subject_token=` — the one line ADR-0020 documents.
- `demo.py` — stands up the real stack (IdP, OPA with a subject-role policy,
  MCP server, gateway), mints one agent token and two user tokens, and sends
  two real HTTP requests through `boundary1_app.py`.
