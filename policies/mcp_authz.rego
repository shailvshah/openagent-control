package openagent.authz

import future.keywords.in

default allow := false

# Tool discovery is always allowed.
allow if {
	input.method == "tools/list"
}

allow if {
	input.method == "tools/call"
	tool := input.params.name
	granted_tools[input.spiffe_id][_] == tool
	tool_check(tool, input.params.arguments)
}

reason := "Capability not granted for this agent identity" if {
	input.method == "tools/call"
	not granted_tools[input.spiffe_id][_] == input.params.name
}

reason := "Tool arguments exceed authorized thresholds" if {
	input.method == "tools/call"
	granted_tools[input.spiffe_id][_] == input.params.name
	not tool_check(input.params.name, input.params.arguments)
}

granted_tools := {
	"spiffe://corp.net/ns/finance/agent/invoice-bot": ["read_query", "update_record"],
	"spiffe://corp.net/ns/sales/agent/lead-qualifier": ["salesforce_update_account"],
}

tool_check("salesforce_update_account", args) if {
	args.credit_limit <= 10000
}

tool_check("read_query", _) := true

tool_check("update_record", _) := true
