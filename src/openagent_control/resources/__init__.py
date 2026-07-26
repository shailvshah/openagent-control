"""Runtime resources shipped inside the package.

These used to live at the repository root, which meant they were absent from a
built wheel and only resolvable if the process happened to be running from a
checkout. A `pip install`ed gateway started, reported healthy, and then failed
every request with `FileNotFoundError: registry/agents.yaml`.

Everything here is resolved through `importlib.resources`, so it works
identically from a source checkout, a wheel, or a zipapp.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config

_PACKAGE = "openagent_control.resources"


def _path(*parts: str) -> Path:
    """Filesystem path to a packaged resource.

    `as_file` would be required for a genuinely zipped import, but Alembic and
    OPA both need real directories on disk, so this package is not zip-safe by
    design and a direct path is the honest interface.
    """
    root = Path(str(resources.files(_PACKAGE)))
    return root.joinpath(*parts)


def default_policy_dir() -> Path:
    """Directory of Rego policies to load into OPA."""
    return _path("policies")


def example_registry() -> Path:
    """A registry file with no agents in it.

    Deliberately empty rather than seeded with demo agents: a fresh install
    must not start life trusting an identity the operator never registered
    (ADR-0008's zero-orphaned-agents rule cuts both ways).
    """
    return _path("agents.example.yaml")


def alembic_ini() -> Path:
    root = Path(str(resources.files("openagent_control")))
    return root / "alembic.ini"


def migrations_dir() -> Path:
    root = Path(str(resources.files("openagent_control")))
    return root / "migrations"


def alembic_config(database_url: str) -> Config:
    """Alembic config pointed at the migrations that shipped in the wheel.

    The packaged ini's `script_location` is relative to the repo layout, so it
    must be overridden — otherwise `migrate` only works from a checkout, which
    is the whole class of bug this module exists to remove.
    """
    from alembic.config import Config as AlembicConfig

    config = AlembicConfig(str(alembic_ini()))
    config.set_main_option("script_location", str(migrations_dir()))
    config.set_main_option("sqlalchemy.url", database_url)
    return config
