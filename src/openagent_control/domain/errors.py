"""Typed domain errors raised across port boundaries.

Adapters translate their infrastructure failures (HTTP errors, socket errors, ...)
into these so the application layer never depends on adapter internals.
"""

from __future__ import annotations


class GovernanceError(Exception):
    """Base class for governed-execution failures."""


class IdentityError(GovernanceError):
    """The calling agent's identity could not be established."""


class MissingSubjectTokenError(GovernanceError):
    """A delegated (on-behalf-of) call arrived without a human subject token."""


class PolicyEngineUnavailableError(GovernanceError):
    """The policy engine could not be reached; callers must fail closed."""


class UpstreamError(GovernanceError):
    """The downstream MCP server rejected or failed the forwarded call."""
