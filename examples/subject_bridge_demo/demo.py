"""Proof for ADR-0020: boundary 1 (who may invoke the agent) and boundary 2
(what the invoked agent may do) are two real, separately-verified checks,
bridged by exactly one thing -- a per-request `subject_token`.

Everything here is real: a real signed-JWT authorization server, a real `opa`
process evaluating a real policy that gates on the user's own role (not just
the agent's registry grant), a real MCP server, the real gateway, and a real
HTTP request into `boundary1_app.py` -- the code ADR-0020 says is the only
wiring a deployer has to write.

Two users call the SAME agent (invoice-bot) through the SAME boundary-1
endpoint. The registry already grants invoice-bot `update_record`; the only
difference between the two calls is which human's token boundary 1 attached:

  * dana@corp.net  -- has the "finance-approver" role  -> ALLOWED
  * intern@corp.net -- does not                         -> DENIED

Neither user is the agent. Neither call changes the agent's own identity or
its registry grant. The only thing that changes is `input.subject.roles`,
read by a real OPA process from a real, independently-verified JWT -- proving
the gateway does not just trust that boundary 1 already checked this.

Run:  poetry run python -m examples.subject_bridge_demo.demo
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx
import uvicorn

from examples.enterprise_scenario.authorization_server import (
    AGENT_CLIENT_ID,
    run_authorization_server,
)
from examples.enterprise_scenario.harness import (
    GATEWAY_AUDIENCE,
    build_settings,
    free_port,
    run_gateway,
    wait_for,
)
from examples.enterprise_scenario.mcp_server import AUDIENCE as MCP_AUDIENCE
from examples.enterprise_scenario.mcp_server import run_mcp_server
from examples.subject_bridge_demo.boundary1_app import build_app

# Registry grants update_record to the agent outright -- policy then narrows
# who may actually trigger it, based on the acting human's own role, not the
# agent's identity. This is ADR-0016's split (registry = allowlist, Rego =
# guardrail) doing double duty for the subject dimension (ADR-0019).
_POLICY = """
package openagent.authz

import rego.v1

default allow := false

allow if {
	input.method == "tools/list"
}

allow if {
	input.method == "tools/call"
	input.agent.status == "active"
	granted(input.params.name)
	not guardrail_violation(input.params.name)
}

granted(tool) if {
	some t in input.agent.granted_tools
	t.name == tool
}

reason := "Capability not granted for this agent identity" if {
	input.method == "tools/call"
	not granted(input.params.name)
}

reason := "Acting user is not entitled to this tool" if {
	input.method == "tools/call"
	granted(input.params.name)
	guardrail_violation(input.params.name)
}

# The null-subject trap ADR-0019 documents: `not "x" in null.roles` is
# undefined, not true, in Rego -- so the no-subject case must be spelled out
# explicitly, or an autonomous call would sail past this guardrail entirely.
guardrail_violation("update_record") if {
	input.subject == null
}

guardrail_violation("update_record") if {
	input.subject != null
	not "finance-approver" in input.subject.roles
}
"""


@contextlib.contextmanager
def run_opa_with_subject_policy():
    if shutil.which("opa") is None:
        raise RuntimeError("this proof needs a real `opa` binary: brew install opa")
    policy_dir = Path(tempfile.mkdtemp())
    (policy_dir / "subject_authz.rego").write_text(_POLICY)
    port = free_port()
    process = subprocess.Popen(
        ["opa", "run", "--server", "--addr", f"127.0.0.1:{port}", str(policy_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}/v1/data/openagent/authz"
    finally:
        process.terminate()
        process.wait(timeout=10)


def _write_registry(path: Path, issuer: str) -> None:
    import yaml

    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "spiffe_id": f"oidc://{issuer}/{AGENT_CLIENT_ID}",
                        "display_name": "Invoice Bot",
                        "purpose": "Reconcile finance invoices.",
                        "owner": "alice@corp.net",
                        "risk_tier": "medium",
                        "status": "active",
                        "granted_tools": ["read_query", "update_record"],
                    }
                ]
            }
        )
    )


def main() -> None:
    with (
        run_authorization_server(GATEWAY_AUDIENCE) as auth,
        run_opa_with_subject_policy() as opa_url,
        run_mcp_server(auth.issuer + "/keys", auth.issuer) as mcp_url,
    ):
        registry_path = Path(tempfile.mkdtemp()) / "agents.yaml"
        _write_registry(registry_path, auth.issuer)

        settings = build_settings(
            auth_discovery_url=auth.discovery_url,
            auth_token_url=auth.token_url,
            opa_url=opa_url,
            mcp_url=mcp_url,
            registry_path=registry_path,
            delegated_audience=MCP_AUDIENCE,
            client_id="openagent-control-gateway",
            client_secret="scenario-only-not-a-real-secret",
        ).model_copy(
            update={
                # ADR-0019: verify the subject token independently, don't just
                # relay it -- the whole point of the boundary-1/2 split.
                "subject_verification_mode": "oidc-jwks",
                "subject_oidc_discovery_url": auth.discovery_url,
                "subject_oidc_audience": GATEWAY_AUDIENCE,
                "subject_binding": "off",  # no sponsor claim in this proof; roles alone decide
            }
        )

        with run_gateway(settings) as gateway_url:
            agent_token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, sponsor=None)

            # boundary1_app.py IS the agent's own serving endpoint -- the code
            # that runs after an API gateway/ASGI middleware already validated
            # the caller's OIDC token. Run it for real, over real HTTP.
            app = build_app(gateway_url, agent_token)
            port = free_port()
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                wait_for(f"http://127.0.0.1:{port}/docs")

                approver_token = auth.mint(
                    GATEWAY_AUDIENCE, {"sub": "dana@corp.net", "roles": ["finance-approver"]}
                )
                intern_token = auth.mint(
                    GATEWAY_AUDIENCE, {"sub": "intern@corp.net", "roles": ["read-only"]}
                )

                print("=" * 72)
                for label, user_token in [
                    ("dana@corp.net (finance-approver)", approver_token),
                    ("intern@corp.net (no approver role)", intern_token),
                ]:
                    response = httpx.post(
                        f"http://127.0.0.1:{port}/invoke",
                        headers={"Authorization": f"Bearer {user_token}"},
                        timeout=10.0,
                    )
                    print(f"[boundary 1 caller: {label}]")
                    print(" ", response.json())
                print("=" * 72)
                print(
                    "Same agent identity, same registry grant, same tool -- the only "
                    "difference was which human's token boundary 1 attached to the "
                    "request. Both decisions were independently verified and receipted "
                    "by the real gateway (docker compose logs gateway / stdout above)."
                )
            finally:
                server.should_exit = True
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
