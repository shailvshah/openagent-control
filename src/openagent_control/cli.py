"""`openagent-control` — the command line a pip install puts on PATH.

Without this a deployer has to know the ASGI import path, hand-run uvicorn, and
find an alembic config that a wheel never shipped. Four commands:

    openagent-control init <dir>   write a starter registry + policy to customise
    openagent-control migrate      create/upgrade the oac schema
    openagent-control doctor       verify config and every dependency, exit non-zero
    openagent-control serve        run the gateway

`doctor` exists because of a failure mode this project keeps rediscovering: a
process that starts, answers /healthz with 200, and then fails every real
request — missing registry file, un-migrated database, unreachable OPA. A
connectivity check alone reproduces that mistake, so it also compares the
database's Alembic revision against head.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from openagent_control.config import Settings
from openagent_control.diagnostics import run_all
from openagent_control.logging_config import configure_logging
from openagent_control.resources import alembic_config, default_policy_dir, example_registry
from openagent_control.tracing import configure_tracing

_OK = "  ok      "
_FAIL = "  FAILED  "


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    target.mkdir(parents=True, exist_ok=True)

    registry = target / "agents.yaml"
    policies = target / "policies"
    if registry.exists() and not args.force:
        print(f"{registry} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    shutil.copy(example_registry(), registry)
    if policies.exists():
        shutil.rmtree(policies)
    shutil.copytree(default_policy_dir(), policies)

    print(f"Wrote {registry}")
    print(f"Wrote {policies}/")
    print()
    print("Next:")
    print(f"  export OAC_REGISTRY_PATH={registry}")
    print(f"  opa run --server {policies}      # or point OAC_OPA_URL at your own")
    print("  openagent-control doctor")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print(
            "OAC_DATABASE_URL is not set — nothing to migrate.\n"
            "The default deployment uses the in-process ledger and a file registry.",
            file=sys.stderr,
        )
        return 1
    try:
        from alembic import command
    except ImportError:
        print(
            "persistence dependencies are not installed — "
            "pip install 'openagent-control[persistence]'",
            file=sys.stderr,
        )
        return 1

    # The schema uses a dedicated `oac` namespace (ADR-0009), which SQLite and
    # MySQL have no equivalent for. Say so, rather than letting the operator
    # hit `near "SCHEMA": syntax error` from inside Alembic.
    if not settings.database_url.startswith("postgresql"):
        print(
            f"OAC_DATABASE_URL uses '{settings.database_url.split('://')[0]}'. "
            "Postgres is the supported backend (ADR-0009): the schema is created "
            "in a dedicated `oac` namespace, which other engines don't support.",
            file=sys.stderr,
        )
        return 1

    command.upgrade(alembic_config(settings.database_url), args.revision)
    print(f"Database is at revision '{args.revision}'.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Runs the same checks GET /readyz runs, so the two cannot disagree."""
    import asyncio

    settings = Settings()
    print("openagent-control doctor\n")
    checks = asyncio.run(run_all(settings))
    for check in checks:
        print(f"{_OK if check.ok else _FAIL}{check.name:9}: {check.detail}")

    print()
    if all(check.ok for check in checks):
        print("All checks passed.")
        return 0
    print("One or more checks FAILED — the gateway would start but not serve.", file=sys.stderr)
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    # This process owns its own logging from here on — see logging_config.py
    # for why this call doesn't live inside create_app() or an adapter.
    configure_logging(args.log_level, json_format=args.log_format == "json")

    settings = Settings()
    if settings.otel_enabled:
        configure_tracing(settings.otel_exporter_endpoint, settings.otel_service_name)

    uvicorn.run(
        "openagent_control.gateway.app:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level.lower(),
    )
    return 0


def cmd_serve_control_plane(args: argparse.Namespace) -> int:
    import uvicorn

    configure_logging(args.log_level, json_format=args.log_format == "json")

    # factory=True: uvicorn calls create_app() itself at server startup rather
    # than this module needing a module-level `app = create_app()`, which
    # would make merely importing control_plane.app fail without
    # OAC_DATABASE_URL set — see the docstring on create_app() for why.
    uvicorn.run(
        "openagent_control.control_plane.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level.lower(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openagent-control",
        description="Agent identity & governance control plane.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter registry and policy set")
    init.add_argument("directory", help="directory to create the files in")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.set_defaults(func=cmd_init)

    migrate = sub.add_parser("migrate", help="create or upgrade the oac schema")
    migrate.add_argument("--revision", default="head")
    migrate.set_defaults(func=cmd_migrate)

    doctor = sub.add_parser("doctor", help="verify config and every dependency")
    doctor.set_defaults(func=cmd_doctor)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - containers bind all
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--log-format",
        choices=["console", "json"],
        default="console",
        help="json emits one structured line per log entry, for a log-aggregation pipeline",
    )
    serve.set_defaults(func=cmd_serve)

    serve_control_plane = sub.add_parser(
        "serve-control-plane", help="run the control-plane API + dashboard (ADR-0014, ADR-0018)"
    )
    serve_control_plane.add_argument("--host", default="0.0.0.0")  # noqa: S104
    serve_control_plane.add_argument("--port", type=int, default=8001)
    serve_control_plane.add_argument("--workers", type=int, default=1)
    serve_control_plane.add_argument("--log-level", default="info")
    serve_control_plane.add_argument(
        "--log-format",
        choices=["console", "json"],
        default="console",
    )
    serve_control_plane.set_defaults(func=cmd_serve_control_plane)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
