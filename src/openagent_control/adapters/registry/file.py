"""File-backed agent registry. See docs/adr/0008-agent-registry-as-source-of-truth.md.

Config-as-code: agents are registered by adding a record to a YAML file reviewed
through normal git workflow. A database- or IdP-backed registry is a later
adapter behind the same `AgentRegistry` port.

The parsed file is cached but re-read when its mtime changes. Caching it for the
process lifetime would mean suspending an agent had no effect until the gateway
restarted — revocation is a control-plane guarantee (ADR-0008), so it cannot
depend on a redeploy. The cost is one `stat` per lookup.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from openagent_control.domain.models import RegisteredAgent


class FileAgentRegistry:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._agents: dict[str, RegisteredAgent] | None = None
        self._loaded_mtime: float | None = None

    def _load(self) -> dict[str, RegisteredAgent]:
        mtime = self._path.stat().st_mtime
        if self._agents is None or mtime != self._loaded_mtime:
            raw = yaml.safe_load(self._path.read_text()) or {}
            agents = [RegisteredAgent.model_validate(entry) for entry in raw.get("agents", [])]
            self._agents = {agent.spiffe_id: agent for agent in agents}
            self._loaded_mtime = mtime
        return self._agents

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        return self._load().get(spiffe_id)
