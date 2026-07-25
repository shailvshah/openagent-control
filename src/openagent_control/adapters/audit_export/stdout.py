"""stdout/log audit exporter. See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.

v1 placeholder for the `AuditExporter` port; real deployments swap this for a
Datadog/Grafana(-OTLP)/Splunk adapter behind the same port. The full receipt is
serialized into the log message itself so it survives any logging formatter — an
audit record carried only in `extra` would be dropped by the default formatter.
"""

from __future__ import annotations

import json
import logging
import sys

from openagent_control.domain.models import ExecutionReceipt

logger = logging.getLogger("openagent_control.audit")


class StdoutAuditExporter:
    def __init__(self) -> None:
        # This adapter's contract is "receipts reach stdout". Host processes
        # (e.g. uvicorn) configure their own loggers and leave the root logger
        # handler-less at WARNING, which would silently drop INFO receipts —
        # so attach a handler explicitly unless one is already present.
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            # propagate stays True: root-level pipelines (pytest caplog, host log
            # shippers) still see receipts; worst case is a duplicate line, never
            # a dropped one.

    async def export(self, receipt: ExecutionReceipt) -> None:
        logger.info("audit_receipt %s", json.dumps(receipt.model_dump(mode="json"), sort_keys=True))
