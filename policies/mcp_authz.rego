# Authorization logic for governed tool calls.
#
# Agent facts (who exists, status, granted tools) come from the Agent Registry
# via input.agent (ADR-0008) — this file holds only the logic. Registering or
# re-scoping an agent is a registry/agents.yaml change, not a policy change.

package openagent.authz

default allow := false

# Tool discovery is always allowed.
allow if {
	input.method == "tools/list"
}

allow if {
	input.method == "tools/call"
	input.agent.status == "active"
	granted(input.params.name)
	tool_check(input.params.name, input.params.arguments)
}

reason := "Capability not granted for this agent identity" if {
	input.method == "tools/call"
	not granted(input.params.name)
}

reason := "Tool arguments exceed authorized thresholds" if {
	input.method == "tools/call"
	granted(input.params.name)
	not tool_check(input.params.name, input.params.arguments)
}

granted(tool) if {
	some t in input.agent.granted_tools
	t == tool
}

# --- Argument-level guardrails (authorization logic, stays in policy) ---

tool_check("salesforce_update_account", args) if {
	args.credit_limit <= 10000
}

tool_check("read_query", _) := true

tool_check("update_record", _) := true
