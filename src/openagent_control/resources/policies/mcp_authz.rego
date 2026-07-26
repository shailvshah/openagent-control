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

granted(tool) if {
	some t in input.agent.granted_tools
	t == tool
}

# --- Argument-level guardrails (authorization logic, stays in policy) ---
#
# Each rule states what is FORBIDDEN for one tool, so a tool with no rule here
# is governed by its registry grant alone. Add a rule to constrain arguments.

guardrail_violation("salesforce_update_account", args) if {
	args.credit_limit > 10000
}
