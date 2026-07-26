from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import pytest
from loguru import logger

from openagent_control.logging_config import configure_logging


@pytest.fixture
def restore_loguru_default_sink() -> Iterator[None]:
    """configure_logging() mutates loguru's process-wide sinks. Restore a
    plain stderr sink afterward so later tests (e.g. the audit exporter's,
    which asserts against stderr) aren't left depending on whatever the last
    test in this file configured."""
    yield
    logger.remove()
    logger.add(sys.stderr)


def test_configure_logging_writes_to_stdout_at_the_given_level(
    capfd: pytest.CaptureFixture[str], restore_loguru_default_sink: None
) -> None:
    configure_logging(level="INFO")

    logger.info("hello from configure_logging")
    logger.debug("should not appear at INFO level")

    out, err = capfd.readouterr()
    assert "hello from configure_logging" in out
    assert "should not appear" not in out
    assert err == ""


def test_configure_logging_json_format_emits_parseable_lines(
    capfd: pytest.CaptureFixture[str], restore_loguru_default_sink: None
) -> None:
    configure_logging(level="INFO", json_format=True)

    logger.info("structured line")

    line = capfd.readouterr().out.strip()
    record = json.loads(line)
    assert record["record"]["message"] == "structured line"


def test_configure_logging_is_safe_to_call_more_than_once(
    capfd: pytest.CaptureFixture[str], restore_loguru_default_sink: None
) -> None:
    """A second call must not leave two sinks writing duplicate lines."""
    configure_logging(level="INFO")
    configure_logging(level="INFO")

    logger.info("only once")

    assert capfd.readouterr().out.count("only once") == 1
