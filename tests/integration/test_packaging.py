"""Packaging conformance: does a real `pip install` actually work?

Builds a wheel, installs it into a clean virtualenv, and runs it **from an
unrelated working directory**. That last part is the whole point. Every test in
this repo otherwise runs from the checkout, where `policies/`, `registry/` and
`migrations/` happen to be on disk — so a wheel that ships none of them passes
the entire suite while a `pip install` starts, answers /healthz with 200, and
fails every request with `FileNotFoundError: registry/agents.yaml`.

Slow (a build plus a venv), so it is opt-in:

    OAC_TEST_PACKAGING=1 poetry run pytest tests/integration/test_packaging.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not os.environ.get("OAC_TEST_PACKAGING"),
    reason="set OAC_TEST_PACKAGING=1 to run the packaging conformance tests (slow)",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    poetry = shutil.which("poetry")
    if poetry is None:
        pytest.skip("poetry is required to build the wheel under test")
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [poetry, "build", "-f", "wheel", "-o", str(out)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


@pytest.fixture(scope="module")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A clean venv with the wheel installed, plus a scratch CWD that is NOT the repo."""
    env_dir = tmp_path_factory.mktemp("venv")
    venv.create(env_dir, with_pip=True)
    pip = env_dir / "bin" / "pip"
    subprocess.run(
        [str(pip), "install", "--quiet", f"{wheel}[persistence]"], check=True, capture_output=True
    )
    yield env_dir


def _run(
    env_dir: Path, *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(env_dir / "bin" / "openagent-control"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "OAC_DATABASE_URL": "", "OAC_REDIS_URL": "", **(env or {})},
    )


def test_wheel_ships_the_files_the_runtime_needs(wheel: Path) -> None:
    """Rego, the starter registry, alembic.ini and the migrations are runtime
    inputs, not developer conveniences — a wheel without them is unusable."""
    names = set(zipfile.ZipFile(wheel).namelist())

    assert "openagent_control/resources/policies/mcp_authz.rego" in names
    assert "openagent_control/resources/agents.example.yaml" in names
    # The dashboard is HTML the control plane reads at runtime (ADR-0018), so
    # it fails exactly the way the Rego and the registry would if omitted.
    assert "openagent_control/resources/dashboard/index.html" in names
    assert "openagent_control/alembic.ini" in names
    assert any(n.startswith("openagent_control/migrations/versions/") for n in names)


def test_wheel_ships_the_sdk_a_client_installs_it_for(wheel: Path) -> None:
    """The SDK is the reason most people will `pip install` this at all
    (ADR-0017) — an agent process installs the package for the client, not the
    server. `langchain.py` must ship too, even though LangChain itself is not a
    dependency: it imports lazily and explains itself if LangChain is absent."""
    names = set(zipfile.ZipFile(wheel).namelist())

    assert "openagent_control/sdk/__init__.py" in names
    assert "openagent_control/sdk/client.py" in names
    assert "openagent_control/sdk/decorator.py" in names
    assert "openagent_control/sdk/langchain.py" in names


def test_cli_is_on_path(installed: Path) -> None:
    result = _run(installed, "--help", cwd=Path.home())

    assert result.returncode == 0
    for command in ("serve", "migrate", "doctor", "init"):
        assert command in result.stdout


def test_app_starts_and_governs_from_an_unrelated_directory(
    installed: Path, tmp_path: Path
) -> None:
    """The regression that motivated this file: import and serve a governed
    call while the working directory has no repo layout in sight."""
    script = tmp_path / "smoke.py"
    script.write_text(
        "from fastapi.testclient import TestClient\n"
        "from openagent_control.gateway.app import create_app\n"
        "c = TestClient(create_app())\n"
        "health = c.get('/healthz').status_code\n"
        "r = c.post('/mcp/v1', headers={'X-Spiffe-ID': 'spiffe://corp.net/ghost'},\n"
        "  json={'jsonrpc':'2.0','id':1,'method':'tools/call',\n"
        "        'params':{'name':'read_query','arguments':{}}})\n"
        "print(__import__('json').dumps({'health': health, 'status': r.status_code,\n"
        "      'body': r.json()}))\n"
    )
    result = subprocess.run(
        [str(installed / "bin" / "python"), str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "OAC_DATABASE_URL": "", "OAC_REDIS_URL": ""},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["health"] == 200
    # The bundled starter registry is empty, so an unregistered agent is denied
    # rather than crashing on a missing file.
    assert "not registered" in payload["body"]["error"]["message"]


def test_init_writes_a_usable_registry_and_policy(installed: Path, tmp_path: Path) -> None:
    result = _run(installed, "init", "./conf", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "conf" / "agents.yaml").is_file()
    assert (tmp_path / "conf" / "policies" / "mcp_authz.rego").is_file()


def test_doctor_reports_a_missing_registry_instead_of_starting_broken(
    installed: Path, tmp_path: Path
) -> None:
    result = _run(installed, "doctor", cwd=tmp_path, env={"OAC_REGISTRY_PATH": "/nope/agents.yaml"})

    assert result.returncode == 1
    assert "does not exist" in result.stdout


def test_migrate_without_a_database_url_explains_itself(installed: Path, tmp_path: Path) -> None:
    result = _run(installed, "migrate", cwd=tmp_path)

    assert result.returncode == 1
    assert "OAC_DATABASE_URL is not set" in result.stderr


def test_migrate_rejects_a_non_postgres_backend_clearly(installed: Path, tmp_path: Path) -> None:
    """Better than `near "SCHEMA": syntax error` from inside Alembic."""
    result = _run(
        installed, "migrate", cwd=tmp_path, env={"OAC_DATABASE_URL": "sqlite+aiosqlite:///./x.db"}
    )

    assert result.returncode == 1
    assert "Postgres is the supported backend" in result.stderr
