"""Process-wide OpenTelemetry configuration for a standalone deployment.

`GovernedExecutionService` (application/governed_execution.py) always pulls its
tracer from `opentelemetry.trace.get_tracer(...)` and emits spans through the
governed-execution path (identify -> registry lookup -> policy evaluate ->
credential broker -> upstream forward) regardless of whether this module has
ever run. Without configuration, the OpenTelemetry API defaults to a no-op
tracer, so instrumentation is unconditional and free; only *exporting* spans
is optional.

Call `configure_tracing()` once, at process startup — the CLI's `serve`
command does this before starting uvicorn, mirroring `logging_config.py`.
Do **not** call it from `create_app()` or any adapter: this project can be
embedded in-process (ADR-0001 Pattern C), and a library that installs a global
TracerProvider just by being imported or constructed would silently override
the host application's own tracing setup.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_tracing(endpoint: str, service_name: str) -> None:
    """Installs a global TracerProvider exporting spans via OTLP/HTTP.

    `endpoint` is the full OTLP HTTP traces path (e.g.
    "http://localhost:4318/v1/traces"), not just a host:port.
    """
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
