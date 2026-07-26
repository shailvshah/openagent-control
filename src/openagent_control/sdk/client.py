"""HTTP clients for the gateway. See ADR-0017.

Sync and async are separate classes rather than one class with a flag: an agent
runtime is usually async and a script usually is not, and a sync method that
secretly blocks an event loop is the kind of bug that only shows up under load.
They share `_build_headers` and the response parsing so the two cannot drift on
anything that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

_DEFAULT_TIMEOUT = 15.0


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class AuthorizationResult:
    """One policy decision, and the receipt that proves it was recorded."""

    allowed: bool
    decision: Decision
    reason: str
    instruction: str
    receipt_id: str
    shadowed: bool = False
    """The policy said DENY but the gateway is in shadow mode (ADR-0012), so
    the call proceeds. Gate on `allowed`, not on `decision`, or you re-enforce
    what shadow mode exists to suspend."""


class GatewayError(RuntimeError):
    """The gateway could not be reached, or answered in a way we can't read.

    Deliberately distinct from a DENY: a denial is the system working. Callers
    that fail closed should catch this and treat it as a denial themselves —
    the SDK will not decide that for them, because in shadow-mode rollouts and
    batch tooling "carry on" is sometimes the correct answer and the SDK cannot
    know which situation it is in.
    """


class _ClientBase:
    def __init__(
        self,
        gateway_url: str,
        *,
        token: str | None = None,
        spiffe_id: str | None = None,
        subject_token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """
        `token` is the agent's own OAuth access token — the production path for
        identity_mode="oidc-jwks"/"jwt-svid". `spiffe_id` sends the X-Spiffe-ID
        header instead, which only works against identity_mode="header", a
        documented dev stub (ADR-0005); it is here so a local first run needs no
        IdP, not as a deployment option.

        `subject_token` is the human sponsor's token for delegated calls
        (RFC 8693 on-behalf-of). Without it, a call the gateway resolves to a
        sponsored identity is refused rather than silently downgraded to an
        autonomous one.
        """
        if not token and not spiffe_id:
            raise ValueError(
                "GovernedClient needs either token= (the agent's access token) or "
                "spiffe_id= (identity_mode='header' only, a dev stub — see ADR-0005)"
            )
        self._base_url = gateway_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        if spiffe_id:
            self._headers["X-Spiffe-ID"] = spiffe_id
        if subject_token:
            self._headers["X-Subject-Token"] = subject_token

    def _authorize_request(
        self, tool: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        return f"{self._base_url}/api/v1/authorize", {"tool": tool, "arguments": arguments}

    def _rpc_request(self, method: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return (
            f"{self._base_url}/mcp/v1",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )


def _parse_authorization(response: httpx.Response) -> AuthorizationResult:
    if response.status_code == 401:
        raise GatewayError(f"gateway refused the agent's identity: {_detail(response)}")
    if response.status_code >= 400:
        raise GatewayError(f"gateway returned {response.status_code}: {_detail(response)}")
    body = response.json()
    return AuthorizationResult(
        allowed=bool(body["allowed"]),
        decision=Decision(body["decision"]),
        reason=body.get("reason", ""),
        instruction=body.get("instruction", ""),
        receipt_id=body["receipt_id"],
        shadowed=bool(body.get("shadowed", False)),
    )


def _parse_rpc(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise GatewayError(f"gateway refused the agent's identity: {_detail(response)}")
    body = response.json()
    if "error" in body:
        error = body["error"]
        instruction = (error.get("data") or {}).get("instruction", "")
        message = error.get("message", "tool call failed")
        raise ToolCallFailed(f"{message}\n\n{instruction}".strip())
    return body.get("result")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        detail = body.get("detail") or (body.get("error") or {}).get("message")
        if detail:
            return str(detail)
    return str(body)[:200]


class ToolCallFailed(RuntimeError):
    """A proxied tool call came back as a JSON-RPC error — a policy denial, a
    credential-brokering failure, or an upstream failure. The message carries
    the gateway's own agent-readable instruction, so surfacing it to a model as
    tool output makes the model stop rather than retry-loop."""


class GovernedClient(_ClientBase):
    """Blocking client. See the package docstring for the two usage shapes."""

    def __init__(
        self, gateway_url: str, *, client: httpx.Client | None = None, **kwargs: Any
    ) -> None:
        """`client` injects a pre-built httpx.Client — the same seam
        OPAPolicyEngine and the identity adapters offer, so a caller can supply
        its own transport, proxy, or mTLS configuration without this class
        growing a parameter per httpx feature."""
        super().__init__(gateway_url, **kwargs)
        self._client = client or httpx.Client(headers=self._headers, timeout=self._timeout)

    def authorize(self, tool: str, arguments: dict[str, Any] | None = None) -> AuthorizationResult:
        """Decides and receipts a call to `tool` without running anything."""
        url, payload = self._authorize_request(tool, arguments or {})
        try:
            return _parse_authorization(self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Runs `tool` on the upstream MCP server, through the gateway."""
        url, payload = self._rpc_request("tools/call", {"name": tool, "arguments": arguments or {}})
        try:
            return _parse_rpc(self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc

    def list_tools(self) -> list[dict[str, Any]]:
        """The tools this agent is granted — already filtered by the gateway to
        the registry's grants (ADR-0016), so it will not list something a call
        would then be denied for."""
        url, payload = self._rpc_request("tools/list", {})
        try:
            result = _parse_rpc(self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc
        tools: list[dict[str, Any]] = (result or {}).get("tools", [])
        return tools

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GovernedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncGovernedClient(_ClientBase):
    """Non-blocking client, for an agent runtime with its own event loop."""

    def __init__(
        self, gateway_url: str, *, client: httpx.AsyncClient | None = None, **kwargs: Any
    ) -> None:
        super().__init__(gateway_url, **kwargs)
        self._client = client or httpx.AsyncClient(headers=self._headers, timeout=self._timeout)

    async def authorize(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> AuthorizationResult:
        url, payload = self._authorize_request(tool, arguments or {})
        try:
            return _parse_authorization(await self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        url, payload = self._rpc_request("tools/call", {"name": tool, "arguments": arguments or {}})
        try:
            return _parse_rpc(await self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        url, payload = self._rpc_request("tools/list", {})
        try:
            result = _parse_rpc(await self._client.post(url, json=payload))
        except httpx.HTTPError as exc:
            raise GatewayError(f"could not reach the gateway at {url}: {exc}") from exc
        tools: list[dict[str, Any]] = (result or {}).get("tools", [])
        return tools

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncGovernedClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
