"""Postgres-backed Agent Registry. See docs/adr/0008 and docs/adr/0009.

The production registry adapter: agent facts live as queryable rows in `oac.agents`
rather than a git-reviewed file, so status is inventoried and (with the caching
layer's short TTL, see adapters/registry/caching.py) close to instantly readable
after a change.

Also implements AgentDirectory (ADR-0014) — the control plane's list/create/
update/set_status surface — on the same class, since it's the same table and
session_factory; Python Protocols are structural, so no special declaration
is needed for one class to satisfy both AgentRegistry and AgentDirectory.
Every mutating AgentDirectory method writes an oac.operator_actions row in
the same transaction as its agents write (ADR-0014's audit-the-admin-surface
requirement).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.tables import AgentRow, OperatorActionRow
from openagent_control.domain.errors import AgentNotFoundError
from openagent_control.domain.models import AgentPatch, AgentStatus, RegisteredAgent, RiskTier


def _to_domain(row: AgentRow) -> RegisteredAgent:
    return RegisteredAgent(
        spiffe_id=row.spiffe_id,
        display_name=row.display_name,
        purpose=row.purpose,
        owner=row.owner,
        risk_tier=RiskTier(row.risk_tier),
        status=AgentStatus(row.status),
        granted_tools=list(row.granted_tools),
        created_at=row.created_at,
        updated_at=row.updated_at,
        status_changed_at=row.status_changed_at,
    )


def _audit_row(
    *, operator_subject: str, action: str, target_spiffe_id: str, detail: dict[str, object]
) -> OperatorActionRow:
    return OperatorActionRow(
        operator_subject=operator_subject,
        action=action,
        target_spiffe_id=target_spiffe_id,
        detail=detail,
        timestamp=datetime.now(UTC),
    )


class PostgresAgentRegistry:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(AgentRow).where(AgentRow.spiffe_id == spiffe_id))
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None

    async def list_agents(self, *, status: AgentStatus | None = None) -> list[RegisteredAgent]:
        async with self._session_factory() as session:
            stmt = select(AgentRow)
            if status is not None:
                stmt = stmt.where(AgentRow.status == status.value)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(row) for row in rows]

    async def create(self, agent: RegisteredAgent, *, operator_subject: str) -> RegisteredAgent:
        async with self._session_factory() as session, session.begin():
            session.add(
                AgentRow(
                    spiffe_id=agent.spiffe_id,
                    display_name=agent.display_name,
                    purpose=agent.purpose,
                    owner=agent.owner,
                    risk_tier=agent.risk_tier.value,
                    status=agent.status.value,
                    granted_tools=[grant.model_dump(mode="json") for grant in agent.granted_tools],
                    created_at=agent.created_at,
                    updated_at=agent.updated_at,
                    status_changed_at=agent.status_changed_at,
                )
            )
            session.add(
                _audit_row(
                    operator_subject=operator_subject,
                    action="agent.create",
                    target_spiffe_id=agent.spiffe_id,
                    detail={"owner": agent.owner, "risk_tier": agent.risk_tier.value},
                )
            )
        return agent

    async def update(
        self, spiffe_id: str, patch: AgentPatch, *, operator_subject: str
    ) -> RegisteredAgent:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(select(AgentRow).where(AgentRow.spiffe_id == spiffe_id))
            ).scalar_one_or_none()
            if row is None:
                raise AgentNotFoundError(spiffe_id)
            # exclude_none: a patch field left unset OR explicitly null means
            # "leave this field alone," not "clear it" — there is no use case
            # for nulling out e.g. owner.
            fields = patch.model_dump(exclude_none=True)
            if "display_name" in fields:
                row.display_name = fields["display_name"]
            if "purpose" in fields:
                row.purpose = fields["purpose"]
            if "owner" in fields:
                row.owner = fields["owner"]
            if patch.risk_tier is not None:
                row.risk_tier = patch.risk_tier.value
            if "granted_tools" in fields:
                row.granted_tools = fields["granted_tools"]
            row.updated_at = datetime.now(UTC)
            session.add(
                _audit_row(
                    operator_subject=operator_subject,
                    action="agent.update",
                    target_spiffe_id=spiffe_id,
                    detail=fields,
                )
            )
            await session.flush()
            return _to_domain(row)

    async def set_status(
        self, spiffe_id: str, status: AgentStatus, *, operator_subject: str
    ) -> RegisteredAgent:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(select(AgentRow).where(AgentRow.spiffe_id == spiffe_id))
            ).scalar_one_or_none()
            if row is None:
                raise AgentNotFoundError(spiffe_id)
            previous_status = row.status
            now = datetime.now(UTC)
            row.status = status.value
            row.status_changed_at = now
            row.updated_at = now
            session.add(
                _audit_row(
                    operator_subject=operator_subject,
                    action="agent.set_status",
                    target_spiffe_id=spiffe_id,
                    detail={"from": previous_status, "to": status.value},
                )
            )
            await session.flush()
            return _to_domain(row)
