"""Dependency checks shared by `openagent-control doctor` and `GET /readyz`.

One implementation, two surfaces: an operator running `doctor` before deploying
and a load balancer polling `/readyz` must agree on what "working" means, or the
CLI blesses a deployment the orchestrator then refuses to route to.

`/healthz` stays deliberately shallow (the process is alive). These are the deep
checks — the ones that distinguish a gateway that is serving from one that is
merely running, which is the failure this project keeps rediscovering.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from openagent_control.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


async def check_registry(settings: Settings) -> Check:
    import yaml

    from openagent_control.gateway.dependencies import resolve_registry_path

    if settings.database_url:
        return Check("registry", True, "backed by Postgres (see the database check)")
    path = resolve_registry_path(settings)
    agents = (yaml.safe_load(path.read_text()) or {}).get("agents") or []
    suffix = (
        " — bundled empty starter, every agent will be denied" if not settings.registry_path else ""
    )
    return Check("registry", True, f"{path}: {len(agents)} agent(s){suffix}")


async def check_opa(settings: Settings) -> Check:
    base = settings.opa_url.split("/v1/")[0]
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{base}/health")
    ok = response.status_code == 200
    return Check("opa", ok, f"{base} -> HTTP {response.status_code}")


async def check_identity(settings: Settings) -> Check:
    if settings.identity_mode != "oidc-jwks":
        return Check("identity", True, f"identity_mode={settings.identity_mode} (no IdP to reach)")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(settings.oidc_discovery_url)
        response.raise_for_status()
    return Check("identity", True, f"discovery ok, issuer={response.json().get('issuer')}")


async def check_database(settings: Settings) -> Check:
    if not settings.database_url:
        return Check("database", True, "unset — in-process ledger + file registry")

    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy.ext.asyncio import create_async_engine

    from openagent_control.resources import alembic_config

    config = alembic_config(settings.database_url)
    head = ScriptDirectory.from_config(config).get_current_head()

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            current = await connection.run_sync(
                lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
            )
    finally:
        await engine.dispose()

    if current == head:
        return Check("database", True, f"schema at head ({head})")
    if current is None:
        return Check("database", False, "no schema — run: openagent-control migrate")
    return Check(
        "database", False, f"schema at {current}, head is {head} — run: openagent-control migrate"
    )


async def check_redis(settings: Settings) -> Check:
    if not settings.redis_url:
        return Check("redis", True, "unset — caching disabled")
    from redis.asyncio import Redis

    client = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    finally:
        await client.aclose()
    return Check("redis", True, "ping ok")


async def check_signing_key(settings: Settings) -> Check:
    if settings.signing_key_mode != "vault-transit":
        return Check(
            "signing_key",
            True,
            "in-process (regenerated on restart, not compliance-grade — see ADR-0013)",
        )

    from openagent_control.adapters.ledger.vault_signer import VaultTransitSigner

    def _connect() -> VaultTransitSigner:
        return VaultTransitSigner(
            vault_url=settings.vault_url,
            token=settings.vault_token,
            key_name=settings.vault_transit_key_name,
        )

    import asyncio

    signer = await asyncio.to_thread(_connect)
    fingerprint = signer.public_key().public_bytes_raw().hex()[:16]
    return Check(
        "signing_key",
        True,
        f"vault-transit key='{settings.vault_transit_key_name}' pubkey={fingerprint}…",
    )


_CHECKS = (
    check_registry,
    check_opa,
    check_identity,
    check_database,
    check_redis,
    check_signing_key,
)


async def run_all(settings: Settings) -> list[Check]:
    """Runs every check, converting a raised error into a failed Check.

    Diagnostics must always produce a report; a traceback out of a readiness
    probe tells the operator nothing about which dependency is down.
    """
    results: list[Check] = []
    for check in _CHECKS:
        name = check.__name__.removeprefix("check_")
        try:
            results.append(await check(settings))
        except Exception as exc:  # noqa: BLE001 — report, never crash
            results.append(Check(name, False, f"{type(exc).__name__}: {exc}"))
    return results
