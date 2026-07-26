"""Unit test for tracing.py's own logic — installing a global TracerProvider.

Whether spans reach a real collector is verified separately in
tests/integration/test_otel_tracing.py; this just proves configure_tracing()
doesn't leave the default no-op tracer in place.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from openagent_control.tracing import configure_tracing


def test_configure_tracing_installs_a_real_tracer_provider() -> None:
    configure_tracing("http://127.0.0.1:1/v1/traces", "oac-unit-test")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Nothing at 127.0.0.1:1 is listening — shut the batch processor's worker
    # thread down immediately rather than let it retry-export against a dead
    # endpoint for the rest of the pytest process.
    provider.shutdown()
