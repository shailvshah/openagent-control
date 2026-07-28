# Authorization logic for governed tool calls.
#
# Agent facts (who exists, status, granted tools) come from the Agent Registry
# via input.agent (ADR-0008) — this file holds only the logic. Registering or
# re-scoping an agent is a registry/agents.yaml change, not a policy change.
#
# The registry grant is the allowlist; the guardrails below only ever *narrow*
# it. A tool with no guardrail rule is allowed once granted. An earlier version
# required an explicit `tool_check` rule per tool, which meant granting a tool
# in the registry was silently not enough — every new tool also needed a Rego
# edit, or it was denied as "arguments exceed authorized thresholds" with no
# arguments involved. That contradicted ADR-0008's "the registry is the source
# of truth for capability", and made adding an upstream (ADR-0016) a
# two-system change instead of one registry entry.

package openagent.authz

default allow := false

# Tool discovery is always allowed. What an agent may *see* is narrowed to its
# registry grants by the gateway itself, not here — see ADR-0016.
allow if {
	input.method == "tools/list"
}

allow if {
	input.method == "tools/call"
	input.agent.status == "active"
	granted(input.params.name)
	not guardrail_violation(input.params.name, input.params.arguments)
}

reason := "Capability not granted for this agent identity" if {
	input.method == "tools/call"
	not granted(input.params.name)
}

reason := "Tool arguments exceed authorized thresholds" if {
	input.method == "tools/call"
	granted(input.params.name)
	guardrail_violation(input.params.name, input.params.arguments)
}

# `granted_tools` entries are objects (`{"name": ..., "risk_tier": ...,
# "approval_required": ...}`, ADR-0021) — a plain string in agents.yaml is
# normalized to `{"name": "..."}` before it ever reaches OPA, so `t.name` is
# always present regardless of how the registry entry was written.
granted(tool) if {
	some t in input.agent.granted_tools
	t.name == tool
}

grant_for(tool) := t if {
	some t in input.agent.granted_tools
	t.name == tool
}

# --- Argument-level guardrails (authorization logic, stays in policy) ---
#
# Each rule states what is FORBIDDEN for one tool, so a tool with no rule here
# is governed by its registry grant alone. Add a rule to constrain arguments.

guardrail_violation("salesforce_update_account", args) if {
	args.credit_limit > 10000
}

# --- Per-grant approval and role requirements (registry-declared, ADR-0021) ---
#
# `approval_required: true` on a grant (set in agents.yaml, not here) means
# that capability may only be exercised on behalf of a verified human — an
# autonomous call carrying this grant is denied outright. `required_roles`
# narrows further, to specific roles on the acting human's own verified token
# (ADR-0019) — the per-tool RBAC tier a registry entry declares once, instead
# of every deployment hand-writing the same null-subject/role rule per
# sensitive tool.
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

requires_delegation(grant) if grant.approval_required

requires_delegation(grant) if count(grant.required_roles) > 0

has_any_role(required, actual) if {
	some role in required
	role in actual
}

# --- The acting user's own entitlements (ADR-0019) ---
#
# `input.subject` is the human this call runs as, verified from their own
# token — set only when OAC_SUBJECT_VERIFICATION_MODE=oidc-jwks and the call
# is delegated. It is NOT the sponsor: sponsorship records who approved the
# agent acting, which is accountability, not permission. Authorization comes
# from the user's own id, roles and scopes.
#
#   input.subject = {"id": "https://idp/realms/corp#a1b2", "roles": [...], "scopes": [...]}
#
# The rules below are commented out because enabling them would change the
# decision for every existing deployment. Uncomment to require that the agent's
# grant AND the user's entitlement both permit a call — the intersection, which
# is the point of delegation:
#
#   guardrail_violation("update_record", _) if {
#   	not "finance-approver" in input.subject.roles
#   }
#
# Absent-subject handling is the part worth getting right. `input.subject` is
# null for an autonomous call, and `not "x" in null.roles` is undefined rather
# than true, so the rule above would NOT fire — an autonomous agent would sail
# past a check meant to constrain it. Say it explicitly instead:
#
#   guardrail_violation("update_record", _) if {
#   	input.subject == null            # nobody's authority to act under
#   }
#
#   guardrail_violation("update_record", _) if {
#   	input.subject != null
#   	not "finance-approver" in input.subject.roles
#   }
#
# Scopes work the same way, and are the better choice when the entitlement is
# already modelled in the IdP as an OAuth scope on the user's own token:
#
#   guardrail_violation("read_query", _) if {
#   	input.subject != null
#   	not "invoices:read" in input.subject.scopes
#   }
