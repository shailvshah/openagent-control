from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openagent_control import cli
from openagent_control.diagnostics import Check
from openagent_control.resources import (
    alembic_config,
    alembic_ini,
    default_policy_dir,
    example_registry,
    migrations_dir,
)


def test_packaged_resources_exist_on_disk() -> None:
    """These are runtime inputs; if they are missing the gateway cannot serve."""
    assert (default_policy_dir() / "mcp_authz.rego").is_file()
    assert example_registry().is_file()
    assert alembic_ini().is_file()
    assert (migrations_dir() / "versions").is_dir()


def test_bundled_registry_is_empty_so_a_fresh_install_trusts_nobody() -> None:
    import yaml

    parsed = yaml.safe_load(example_registry().read_text())

    assert parsed["agents"] == []


def test_alembic_config_points_at_the_packaged_migrations() -> None:
    config = alembic_config("postgresql+asyncpg://u:p@h/db")

    assert config.get_main_option("script_location") == str(migrations_dir())
    assert config.get_main_option("sqlalchemy.url") == "postgresql+asyncpg://u:p@h/db"


def test_init_writes_registry_and_policies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init", str(tmp_path / "conf")]) == 0

    assert (tmp_path / "conf" / "agents.yaml").is_file()
    assert (tmp_path / "conf" / "policies" / "mcp_authz.rego").is_file()
    assert "Next:" in capsys.readouterr().out


def test_init_refuses_to_clobber_without_force(tmp_path: Path) -> None:
    target = tmp_path / "conf"
    cli.main(["init", str(target)])

    assert cli.main(["init", str(target)]) == 1
    assert cli.main(["init", str(target), "--force"]) == 0


def test_migrate_without_a_database_url_is_a_clear_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OAC_DATABASE_URL", "")

    assert cli.main(["migrate"]) == 1
    assert "OAC_DATABASE_URL is not set" in capsys.readouterr().err


def test_migrate_rejects_non_postgres_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise the operator gets `near "SCHEMA": syntax error` from Alembic."""
    monkeypatch.setenv("OAC_DATABASE_URL", "sqlite+aiosqlite:///./x.db")

    assert cli.main(["migrate"]) == 1
    assert "Postgres is the supported backend" in capsys.readouterr().err


def test_migrate_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAC_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    called: dict[str, Any] = {}

    def fake_upgrade(config: Any, revision: str) -> None:
        called["revision"] = revision

    monkeypatch.setattr("alembic.command.upgrade", fake_upgrade)

    assert cli.main(["migrate"]) == 0
    assert called["revision"] == "head"


def test_doctor_exits_zero_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def all_ok(_settings: Any) -> list[Check]:
        return [Check("registry", True, "fine"), Check("opa", True, "fine")]

    monkeypatch.setattr(cli, "run_all", all_ok)

    assert cli.main(["doctor"]) == 0
    assert "All checks passed." in capsys.readouterr().out


def test_doctor_exits_nonzero_when_a_dependency_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def one_bad(_settings: Any) -> list[Check]:
        return [Check("database", False, "no schema — run: openagent-control migrate")]

    monkeypatch.setattr(cli, "run_all", one_bad)

    assert cli.main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "no schema" in captured.out
    assert "would start but not serve" in captured.err


def test_serve_invokes_uvicorn_with_the_requested_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr("uvicorn.run", fake_run)
    # configure_logging mutates loguru's global sinks — never let a test
    # actually run it, or it leaks into every test that runs afterward in the
    # same pytest process.
    monkeypatch.setattr("openagent_control.cli.configure_logging", lambda *a, **k: None)

    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "9999", "--workers", "3"]) == 0
    assert captured["app"] == "openagent_control.gateway.app:app"
    assert (captured["host"], captured["port"], captured["workers"]) == ("127.0.0.1", 9999, 3)


def test_serve_configures_loguru_before_starting_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    configured: dict[str, Any] = {}
    monkeypatch.setattr(
        "openagent_control.cli.configure_logging",
        lambda level, json_format: configured.update(level=level, json_format=json_format),
    )

    assert cli.main(["serve", "--log-level", "debug", "--log-format", "json"]) == 0

    assert configured == {"level": "debug", "json_format": True}


def test_serve_does_not_configure_tracing_when_otel_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    monkeypatch.setattr("openagent_control.cli.configure_logging", lambda *a, **k: None)
    called = False

    def fake_configure_tracing(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("openagent_control.cli.configure_tracing", fake_configure_tracing)

    assert cli.main(["serve"]) == 0
    assert called is False


def test_serve_configures_tracing_when_otel_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    monkeypatch.setattr("openagent_control.cli.configure_logging", lambda *a, **k: None)
    monkeypatch.setenv("OAC_OTEL_ENABLED", "true")
    monkeypatch.setenv("OAC_OTEL_EXPORTER_ENDPOINT", "http://collector:4318/v1/traces")
    monkeypatch.setenv("OAC_OTEL_SERVICE_NAME", "oac-test")
    configured: dict[str, Any] = {}
    monkeypatch.setattr(
        "openagent_control.cli.configure_tracing",
        lambda endpoint, service_name: configured.update(
            endpoint=endpoint, service_name=service_name
        ),
    )

    assert cli.main(["serve"]) == 0

    assert configured == {
        "endpoint": "http://collector:4318/v1/traces",
        "service_name": "oac-test",
    }


def test_serve_control_plane_invokes_uvicorn_with_factory_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr("openagent_control.cli.configure_logging", lambda *a, **k: None)

    assert (
        cli.main(["serve-control-plane", "--host", "127.0.0.1", "--port", "9001", "--workers", "2"])
        == 0
    )

    assert captured["app"] == "openagent_control.control_plane.app:create_app"
    assert captured["factory"] is True
    assert (captured["host"], captured["port"], captured["workers"]) == ("127.0.0.1", 9001, 2)
