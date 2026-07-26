"""Shared FastAPI dependency: resolves the calling operator's identity via
whichever OperatorIdentity adapter the container was built with, mapping
IdentityError to a 401. See docs/adr/0014.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from openagent_control.control_plane.dependencies import ControlPlaneContainer, get_container
from openagent_control.domain.errors import IdentityError


async def get_operator_subject(
    request: Request, container: Annotated[ControlPlaneContainer, Depends(get_container)]
) -> str:
    try:
        return await container.operator_auth.identify(dict(request.headers))
    except IdentityError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
