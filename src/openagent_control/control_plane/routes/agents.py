"""Agent registry CRUD for the control plane. See docs/adr/0014."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from openagent_control.control_plane.auth import get_operator_subject
from openagent_control.control_plane.dependencies import ControlPlaneContainer, get_container
from openagent_control.domain.errors import AgentNotFoundError
from openagent_control.domain.models import AgentPatch, AgentStatus, RegisteredAgent, RiskTier

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_Container = Annotated[ControlPlaneContainer, Depends(get_container)]
_Operator = Annotated[str, Depends(get_operator_subject)]


class CreateAgentRequest(BaseModel):
    spiffe_id: str
    display_name: str
    purpose: str
    owner: str
    risk_tier: RiskTier
    granted_tools: list[str] = Field(default_factory=list)


@router.get("")
async def list_agents(
    container: _Container, _operator: _Operator, status_filter: AgentStatus | None = None
) -> list[RegisteredAgent]:
    return await container.agent_directory.list_agents(status=status_filter)


@router.get("/{spiffe_id:path}")
async def get_agent(spiffe_id: str, container: _Container, _operator: _Operator) -> RegisteredAgent:
    agent = await container.agent_registry.lookup(spiffe_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no agent registered as '{spiffe_id}'")
    return agent


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest, container: _Container, operator: _Operator
) -> RegisteredAgent:
    agent = RegisteredAgent(
        spiffe_id=body.spiffe_id,
        display_name=body.display_name,
        purpose=body.purpose,
        owner=body.owner,
        risk_tier=body.risk_tier,
        granted_tools=body.granted_tools,
    )
    return await container.agent_directory.create(agent, operator_subject=operator)


@router.patch("/{spiffe_id:path}")
async def update_agent(
    spiffe_id: str, patch: AgentPatch, container: _Container, operator: _Operator
) -> RegisteredAgent:
    try:
        return await container.agent_directory.update(spiffe_id, patch, operator_subject=operator)
    except AgentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{spiffe_id:path}/suspend")
async def suspend_agent(
    spiffe_id: str, container: _Container, operator: _Operator
) -> RegisteredAgent:
    try:
        return await container.agent_directory.set_status(
            spiffe_id, AgentStatus.SUSPENDED, operator_subject=operator
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{spiffe_id:path}/activate")
async def activate_agent(
    spiffe_id: str, container: _Container, operator: _Operator
) -> RegisteredAgent:
    try:
        return await container.agent_directory.set_status(
            spiffe_id, AgentStatus.ACTIVE, operator_subject=operator
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
