# ADR-0021: Per-grant metadata — risk tier, approval, and required roles on `granted_tools`

## Status
Accepted

## Context
Since ADR-0008, `RegisteredAgent.granted_tools` has been a flat list of tool-name
strings: the allowlist, sufficient on its own, narrowed only by guardrails written
in Rego (ADR-0016). That models one agent granted a flat set of capabilities well,
but it doesn't match how an enterprise actually tiers access once an agent has more
than a couple of tools: `read_query` and `update_record` on the same invoice-bot
rarely warrant the same scrutiny, and "who may trigger this" is frequently a
per-capability question, not a per-agent one — a junior analyst's agent might
read invoices for anyone; only a finance approver's delegated call should update
one.

Before this change, expressing that distinction meant hand-writing a Rego rule per
sensitive tool, per deployment — exactly the pattern ADR-0008 moved away from for
agent facts, now recurring one layer down for tool grants. ADR-0019 already
verifies *who* a delegated call runs as (`input.subject`); nothing connected that
identity to *which tools* require it, short of bespoke policy code every enterprise
would have to write and maintain independently.

## Decision
`granted_tools` entries may now be a plain tool name (unchanged) or a `ToolGrant`
object carrying per-grant terms:

```yaml
granted_tools:
  - read_query                    # plain grant: no extra terms, as before
  - name: update_record           # object form: per-grant terms
    required_roles: [finance-approver]
    risk_tier: high
```

`RegisteredAgent.granted_tools` and `AgentPatch.granted_tools` normalize a bare
string to `ToolGrant(name=...)` before validation
(`domain.models.normalize_tool_grants`), so **every registry file written before
this ADR still parses unchanged** — this is additive, not a migration.

`ToolGrant` fields:
- `name: str` — required, matches a tool name as `granted_tools` always has.
- `risk_tier: RiskTier | None` — overrides the agent's own `risk_tier` for this one
  capability. Descriptive/audit-facing only (surfaced on the dashboard), the same
  as the agent-level `risk_tier` today; not itself enforced by the default policy.
- `approval_required: bool` — when true, this grant may only be exercised on a
  delegated call (`input.subject != null`, ADR-0019's verified human token); an
  autonomous call using this grant is denied outright.
- `required_roles: list[str]` — narrows `approval_required` from "any verified
  human" to "a human holding one of these roles," read from the subject's own
  verified `roles` claim. A non-empty list implies delegation is required, same as
  `approval_required=True`. Role *names* are whatever the enterprise's IdP calls
  them (an Okta group name, an Entra app role, a Keycloak realm role) — this
  project doesn't invent a vocabulary, it only compares strings the deployer
  configured via `OAC_SUBJECT_ROLE_CLAIM` (ADR-0019).

**Enforcement lives in the shipped policy, not bespoke Rego per deployment.**
`resources/policies/mcp_authz.rego`'s `granted()` rule now matches on `t.name`
(objects, always — a plain string is normalized before Rego ever sees it), and two
new guardrail rules read `approval_required`/`required_roles` straight off the
matching grant:

```rego
guardrail_violation(tool, _) if {
	grant := grant_for(tool)
	requires_delegation(grant)
	input.subject == null
}

guardrail_violation(tool, _) if {
	grant := grant_for(tool)
	count(grant.required_roles) > 0
	input.subject != null
	not has_any_role(grant.required_roles, input.subject.roles)
}
```

This is registry data driving generic policy logic — the same shape ADR-0008
established for agent facts, extended one level down to the grant.

## Consequences
- **Backward compatible by construction**: every existing `agents.yaml`, including
  `registry/agents.yaml` in this repo, keeps working with no edits — verified by
  `tests/unit/test_registry_file.py::test_plain_string_and_object_grants_both_parse`.
- **Real enforcement, verified against a real `opa` process**:
  `tests/integration/test_rego_policy.py` adds four cases against the actual
  shipped policy (`approval_required` denies/allows on subject presence,
  `required_roles` denies/allows on role match) — not just unit tests against a
  mocked policy engine.
- **Dashboard surfaces per-grant terms**: the agent table's "Granted tools" column
  now marks `approval_required`/`required_roles` grants with `*` and lists the
  required roles, so an operator can see which capabilities are gated without
  opening the registry file.
- **`risk_tier` on a grant is descriptive, not enforced** — same posture as the
  agent-level field today. Adding enforcement (e.g. requiring a stronger approval
  chain for `risk_tier: high` grants) is a real future extension, but would be a
  separate, deliberate policy decision, not a side effect of adding the field.
- **Storage**: the Postgres `agents.granted_tools` column is untyped `JSON`
  already (no migration needed); a legacy row holding plain strings normalizes the
  same way a legacy YAML entry does, the moment it's next read through
  `RegisteredAgent`.
- **Not addressed**: rate limiting per grant (call volume, not just who/whether) —
  raised during design but deliberately left out. It would need call-history state
  the policy engine doesn't have today (OPA is stateless per decision); adding a
  field for it now without enforcement would be exactly the kind of half-finished
  capability this project avoids. Revisit if a real deployment needs it, informed
  by where the counting actually happens (ledger-backed, most likely).
