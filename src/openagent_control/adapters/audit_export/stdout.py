"""stdout/log audit exporter. See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.

v1 placeholder for the `AuditExporter` port; real deployments swap this for a
Datadog/Grafana(-OTLP)/Splunk adapter behind the same port.
"""

from __future__ import annotations

import logging

from openagent_control.domain.models import ExecutionReceipt

logger = logging.getLogger("openagent_control.audit")


class StdoutAuditExporter:
    async def export(self, receipt: ExecutionReceipt) -> None:
        logger.info("audit_receipt", extra={"receipt": receipt.model_dump(mode="json")})
