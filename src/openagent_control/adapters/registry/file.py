"""File-backed agent registry. See docs/adr/0008-agent-registry-as-source-of-truth.md.

Config-as-code: agents are registered by adding a record to a YAML file reviewed
through normal git workflow. The file is parsed once and cached; a database- or
IdP-backed registry is a later adapter behind the same `AgentRegistry` port.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from openagent_control.domain.models import RegisteredAgent


class FileAgentRegistry:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._agents: dict[str, RegisteredAgent] | None = None

    def _load(self) -> dict[str, RegisteredAgent]:
        if self._agents is None:
            raw = yaml.safe_load(self._path.read_text()) or {}
            agents = [RegisteredAgent.model_validate(entry) for entry in raw.get("agents", [])]
            self._agents = {agent.spiffe_id: agent for agent in agents}
        return self._agents

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        return self._load().get(spiffe_id)
