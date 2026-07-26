"""`@governed` — a policy check and a signed receipt in front of a function you
already wrote. See ADR-0017.

The tool name defaults to the function's own name, because that is what the
registry and the policy already talk about; `name=` overrides it when the two
genuinely differ. The arguments sent for evaluation are the call's **bound**
arguments, resolved through the function's signature, so a policy guardrail on
`credit_limit` sees the same value whether the caller passed it positionally or
by keyword. Forwarding only `**kwargs` would let `update_account("ACC-1", 50000)`
slip past a threshold rule that `update_account(credit_limit=50000)` trips — a
policy bypass reachable by changing nothing but call style.

**Denial behaviour is a choice, and the right answer differs by caller.** In
plain Python, raising is correct: the function did not run, and a return value
would be a lie. Inside an agent framework it is wrong — an exception propagates
out of the tool and crashes the graph, where what you want is for the model to
*read* the denial and stop. So `on_deny="raise"` (the default) raises
`ToolCallDenied`, and `on_deny="return"` returns the gateway's own
agent-readable instruction as the tool's output. `openagent_control.sdk.langchain`
defaults to "return" for exactly this reason.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar, cast

from openagent_control.sdk.client import AsyncGovernedClient, AuthorizationResult, GovernedClient

F = TypeVar("F", bound=Callable[..., Any])

OnDeny = Literal["raise", "return"]


class ToolCallDenied(PermissionError):
    """Policy refused this call, so the wrapped function never ran.

    Subclasses PermissionError so an agent's existing `except PermissionError`
    handling does something sensible without being taught a new type.
    """

    def __init__(self, result: AuthorizationResult, tool: str) -> None:
        self.result = result
        self.tool = tool
        super().__init__(denial_text(result, tool))


def denial_text(result: AuthorizationResult, tool: str) -> str:
    """The message a model should see: what was refused, why, and what to do
    about it. The instruction comes from the gateway, not from here, so an
    operator who retunes it in policy retunes what every agent reads."""
    message = f"BLOCKED: '{tool}' denied by policy — {result.reason}"
    if result.instruction:
        message = f"{message}\n\n{result.instruction}"
    return message


def _bind(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Resolves a call's arguments to {name: value}, positional or keyword.

    Falls back to the keyword arguments alone when the call doesn't match the
    signature: raising here would replace a plain TypeError with a confusing
    governance error, and the function itself is about to raise the real one.
    """
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
    except TypeError:
        return dict(kwargs)
    bound.apply_defaults()
    arguments = dict(bound.arguments)
    arguments.pop("self", None)
    return arguments


def _check_decorable(func: Any) -> None:
    """Rejects anything that isn't a plain function, with the fix in the error.

    The order `@governed` / `@tool` is easy to get backwards, and backwards it
    would wrap a framework's tool *object*: functools.wraps would copy the
    wrong metadata, iscoroutinefunction would answer False for an async tool,
    and the failure would surface far from its cause.
    """
    if not (inspect.isfunction(func) or inspect.ismethod(func)):
        raise TypeError(
            f"@governed expects a plain function, got {type(func).__name__}. "
            "If you are wrapping a framework tool, @governed must be the INNER "
            "decorator:\n\n    @tool\n    @governed(oac)\n    def my_tool(...): ..."
        )


def governed(
    client: GovernedClient | AsyncGovernedClient,
    *,
    name: str | None = None,
    on_deny: OnDeny = "raise",
) -> Callable[[F], F]:
    """Gates a function on a real policy decision from the gateway.

    Works on sync and async functions; an async function must be paired with an
    `AsyncGovernedClient`, or the authorization round trip would block the
    caller's event loop.
    """

    def decorate(func: F) -> F:
        _check_decorable(func)
        tool = name or func.__name__

        if inspect.iscoroutinefunction(func):
            if not isinstance(client, AsyncGovernedClient):
                raise TypeError(
                    f"@governed on async function '{func.__name__}' needs an "
                    "AsyncGovernedClient; a GovernedClient would block the event "
                    "loop on every authorization call"
                )
            async_client = client

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                result = await async_client.authorize(tool, _bind(func, args, kwargs))
                if not result.allowed:
                    if on_deny == "return":
                        return denial_text(result, tool)
                    raise ToolCallDenied(result, tool)
                return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)

            return cast(F, async_wrapper)

        if not isinstance(client, GovernedClient):
            raise TypeError(
                f"@governed on sync function '{func.__name__}' needs a GovernedClient; "
                "an AsyncGovernedClient's authorize() returns a coroutine this "
                "wrapper has no running loop to await"
            )
        sync_client = client

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = sync_client.authorize(tool, _bind(func, args, kwargs))
            if not result.allowed:
                if on_deny == "return":
                    return denial_text(result, tool)
                raise ToolCallDenied(result, tool)
            return func(*args, **kwargs)

        return cast(F, wrapper)

    return decorate
