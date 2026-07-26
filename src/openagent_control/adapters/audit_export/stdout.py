"""stdout/log audit exporter. See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.

v1 placeholder for the `AuditExporter` port; real deployments swap this for a
Datadog/Grafana(-OTLP)/Splunk adapter behind the same port. The full receipt is
serialized into the log message itself so it survives any log processor — an
audit record carried only as structured `extra` context could be dropped by a
formatter that doesn't know to render it.

Uses loguru rather than stdlib `logging`, which is why this got simpler: the
previous version had to detect and attach a `logging.StreamHandler` at
construction time, because a host process (uvicorn, or an app this is
embedded into per ADR-0001 Pattern C) commonly leaves the root logger
handler-less at WARNING — which silently drops INFO-level receipts. loguru
ships with a working default sink (stderr) from the moment it is imported, so
that failure mode doesn't exist here: a receipt is never dropped just because
nobody configured logging.

A standalone deployment routes this — and everything else logged through
loguru — to stdout at a chosen level/format by calling
`openagent_control.logging_config.configure_logging()` once at process start
(the CLI's `serve` command does). `export()` is correct either way.
"""

from __future__ import annotations

import json

from loguru import logger

from openagent_control.domain.models import ExecutionReceipt


class StdoutAuditExporter:
    async def export(self, receipt: ExecutionReceipt) -> None:
        logger.info("audit_receipt {}", json.dumps(receipt.model_dump(mode="json"), sort_keys=True))
