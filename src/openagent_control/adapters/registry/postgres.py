"""Postgres-backed Agent Registry. See docs/adr/0008 and docs/adr/0009.

The production registry adapter: agent facts live as queryable rows in `oac.agents`
rather than a git-reviewed file, so status is inventoried and (with the caching
layer's short TTL, see adapters/registry/caching.py) close to instantly readable
after a change — the capability an admin kill-switch feature would build on.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openagent_control.adapters.db.tables import AgentRow
from openagent_control.domain.models import AgentStatus, RegisteredAgent, RiskTier


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


class PostgresAgentRegistry:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(AgentRow).where(AgentRow.spiffe_id == spiffe_id))
            ).scalar_one_or_none()
            return _to_domain(row) if row is not None else None
