"""GovernedExecutionService spans against a real local OTel collector.

Same standard the rest of this repo holds integration work to: verify against
the real thing. `opentelemetry-sdk`'s in-memory exporters would prove this
project calls the API correctly; they would not prove a real collector can
actually parse what gets sent over the wire — OTLP/HTTP protobuf encoding,
resource attributes, parent/child span linkage.

Skips if the `otelcol` binary is unavailable. There's no Homebrew formula for
it; download a release directly:
https://github.com/open-telemetry/opentelemetry-collector-releases/releases
(grab the plain "otelcol" asset, not "-contrib", for your platform, and put it
on PATH).
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.application.governed_execution import GovernedExecutionService
from openagent_control.domain.models import (
    AgentStatus,
    Decision,
    PolicyDecision,
    RegisteredAgent,
    RiskTier,
    ToolCallRequest,
)
from openagent_control.tracing import configure_tracing

pytestmark = pytest.mark.skipif(
    shutil.which("otelcol") is None,
    reason="requires the real `otelcol` binary — see module docstring for install",
)

_AGENT = "spiffe://corp.net/ns/finance/agent/invoice-bot"
_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "read_query", "arguments": {"table": "invoices"}},
}


class _FakePolicyEngine:
    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        return PolicyDecision(decision=Decision.ALLOW, reason="ok")


class _FakeRegistry:
    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        return RegisteredAgent(
            spiffe_id=spiffe_id,
            display_name="Invoice Bot",
            purpose="demo",
            owner="alice@corp.net",
            risk_tier=RiskTier.MEDIUM,
            status=AgentStatus.ACTIVE,
            granted_tools=["read_query"],
        )


class _FakeExporter:
    async def export(self, receipt: Any) -> None:
        pass


class _FakeTokenExchange:
    async def exchange(self, subject_token: str, audience: str) -> str:
        return f"obo::{subject_token}::{audience}"


class _FakeUpstream:
    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request.request_id, "result": "ok"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.contextmanager
def run_otel_collector(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Starts a real `otelcol` with an OTLP/HTTP receiver and a debug exporter
    that dumps every received span to a log file. Yields (endpoint, log_path)."""
    port = _free_port()
    config_path = tmp_path / "otelcol-config.yaml"
    log_path = tmp_path / "otelcol.log"
    config_path.write_text(
        f"""
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{port}
exporters:
  debug:
    verbosity: detailed
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
"""
    )
    with log_path.open("w") as log_fh:
        process = subprocess.Popen(
            ["otelcol", f"--config={config_path}"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if "Everything is ready" in log_path.read_text():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("otelcol did not become ready")

            yield f"http://127.0.0.1:{port}/v1/traces", log_path
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.asyncio
async def test_governed_execution_spans_reach_a_real_local_collector(tmp_path: Path) -> None:
    with run_otel_collector(tmp_path) as (endpoint, log_path):
        configure_tracing(endpoint, "oac-integration-test")

        service = GovernedExecutionService(
            identity_provider=HeaderIdentityProvider(),
            agent_registry=_FakeRegistry(),
            policy_engine=_FakePolicyEngine(),
            ledger=Ed25519ChainLedger(),
            audit_exporter=_FakeExporter(),
            token_exchange=_FakeTokenExchange(),
            mcp_upstream=_FakeUpstream(),
            delegated_audience="test-audience",
        )

        result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)
        assert result["result"] == "ok"

        provider = trace.get_tracer_provider()
        assert isinstance(provider, SdkTracerProvider)
        provider.force_flush()

        deadline = time.monotonic() + 5.0
        log_text = ""
        while time.monotonic() < deadline:
            log_text = log_path.read_text()
            if "governed_execution.execute" in log_text:
                break
            time.sleep(0.1)

        for expected_span in (
            "identify",
            "registry.lookup",
            "policy_evaluate",
            "broker_credential",
            "forward",
            "governed_execution.execute",
        ):
            assert f"Name           : {expected_span}" in log_text

        # The root span carries the decision the call actually reached.
        assert "policy.decision: Str(ALLOW)" in log_text
        assert "tool.name: Str(read_query)" in log_text

    # The collector above is now terminated — shut the exporter down so its
    # background thread doesn't spend the rest of the pytest process
    # retrying against a dead endpoint.
    provider = trace.get_tracer_provider()
    assert isinstance(provider, SdkTracerProvider)
    provider.shutdown()
