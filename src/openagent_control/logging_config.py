"""Process-wide loguru configuration for a standalone deployment.

Every adapter that logs in this project (currently `StdoutAuditExporter`) uses
loguru directly and works correctly with zero configuration, because loguru's
default sink (stderr) exists from import. This module is the *optional*
upgrade a real deployment reaches for: one stdout sink, one chosen level, and
optionally structured JSON lines for a log-aggregation pipeline
(Datadog/Grafana, per ADR-0006's Observability/SIEM port category).

Call `configure_logging()` once, at process startup — the CLI's `serve`
command does this before starting uvicorn. Do **not** call it from
`create_app()`, from any adapter, or from anywhere reachable at import time:
this project can be embedded in-process (ADR-0001 Pattern C), and a library
that reconfigures the host application's logging just by being imported or
constructed is a bad citizen. Reconfiguring logging is something only the
process's actual entrypoint gets to decide.
"""

from __future__ import annotations

import sys

from loguru import logger


def configure_logging(level: str = "INFO", json_format: bool = False) -> None:
    """Replaces loguru's default stderr sink with one stdout sink at `level`.

    `json_format=True` emits one JSON object per line (loguru's `serialize`)
    for a log shipper to parse; the default is human-readable, for an operator
    watching a terminal or `docker compose logs`.
    """
    logger.remove()
    logger.add(
        sys.stdout,
        level=level.upper(),
        serialize=json_format,
        backtrace=False,
        diagnose=False,
    )
