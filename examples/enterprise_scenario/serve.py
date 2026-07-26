"""Runs the scenario's authorization server and MCP server as a long-lived
process, so `docker compose up` exercises the same real components as
`scenario.py` instead of a fixed-string echo container.

Both listen on stable ports and use a configured issuer URL, because `iss` is
validated by the gateway and by the MCP server -- it has to match the address
clients actually reach, which inside compose is a service hostname.

On startup it prints a ready-to-use agent token and sponsor token, since with
real OIDC identity there is no longer any such thing as an unauthenticated
example request.

Env:
  OAC_SCENARIO_ISSUER    public base URL of the authorization server
                         (default http://enterprise-backend:8090)
  OAC_SCENARIO_AUTH_PORT  default 8090
  OAC_SCENARIO_MCP_PORT   default 8080
  OAC_SCENARIO_AUDIENCE   gateway audience (default api://openagent-control-gateway)
"""

from __future__ import annotations

import os
import threading

from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.authorization_server import (
    AGENT_CLIENT_ID,
    JWKS_PATH,
    build_authorization_server,
)

HUMAN_SPONSOR = "dana.reed@corp.net"


def main() -> None:
    issuer = os.environ.get("OAC_SCENARIO_ISSUER", "http://enterprise-backend:8090")
    auth_port = int(os.environ.get("OAC_SCENARIO_AUTH_PORT", "8090"))
    mcp_port = int(os.environ.get("OAC_SCENARIO_MCP_PORT", "8080"))
    audience = os.environ.get("OAC_SCENARIO_AUDIENCE", "api://openagent-control-gateway")

    auth_http, auth = build_authorization_server(
        audience, host="0.0.0.0", port=auth_port, issuer=issuer
    )
    threading.Thread(target=auth_http.serve_forever, daemon=True).start()

    mcp_http = mcp.build_mcp_server(
        issuer + JWKS_PATH, issuer, host="0.0.0.0", port=mcp_port
    )

    agent_token = auth.mint_agent_token(audience, AGENT_CLIENT_ID, HUMAN_SPONSOR)
    sponsor_token = auth.mint_sponsor_token(audience, HUMAN_SPONSOR)

    print(f"authorization server : {issuer}", flush=True)
    print(f"MCP server           : port {mcp_port} (audience {mcp.AUDIENCE})", flush=True)
    print(f"registry spiffe_id   : oidc://{issuer}/{AGENT_CLIENT_ID}", flush=True)
    print(f"\nAGENT_TOKEN={agent_token}\n", flush=True)
    print(f"SUBJECT_TOKEN={sponsor_token}\n", flush=True)

    mcp_http.serve_forever()


if __name__ == "__main__":
    main()
