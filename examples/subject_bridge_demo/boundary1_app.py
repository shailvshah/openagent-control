"""The agent's own serving endpoint -- the code that runs *after* boundary 1
(an API gateway or ASGI middleware) has already validated the caller's OIDC
token. See ADR-0020's "actual wiring" section: this is that one line, not a
new mechanism.

In a real deployment, whatever already validated the human's token at the
edge would typically strip it back out or replace it with something narrower
before this handler runs. This demo forwards the raw bearer through
unchanged, on purpose -- so the same JWT that a real boundary-1 gateway
validated once is also independently re-verified here, at boundary 2
(`OidcSubjectVerifier` never trusts that boundary 1 already checked it; see
ADR-0019).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from openagent_control.sdk import GovernedClient
from openagent_control.sdk.client import GatewayError, ToolCallFailed


def build_app(gateway_url: str, agent_token: str) -> FastAPI:
    app = FastAPI()

    @app.post("/invoke")
    def invoke(authorization: str = Header(...)) -> dict[str, str]:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "expected 'Authorization: Bearer <user token>'")
        user_token = authorization.split(" ", 1)[1]

        # The one line ADR-0020 documents: per-request subject_token, derived
        # from whatever boundary 1 attached to this request -- not a proxy,
        # not new infrastructure, just reading a header already there.
        oac = GovernedClient(gateway_url, token=agent_token, subject_token=user_token)
        try:
            result = oac.call_tool(
                "update_record", {"invoice_id": "INV-1001", "status": "reconciled"}
            )
        except (ToolCallFailed, GatewayError) as exc:
            return {"decision": "DENIED", "detail": str(exc)}
        return {"decision": "ALLOWED", "result": str(result)}

    return app
