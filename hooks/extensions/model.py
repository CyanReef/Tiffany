"""Shared keys and immutable command contributions; no platform branches."""
from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Awaitable, Callable

from core import Context, Field, ServiceKey
from fields import MESSAGE_TYPE, USER_ID
from hooks.command import Command


Handler = Callable[[Context, Command], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    handler: Handler
    needs: tuple[Field, ...] = ()
    uses: tuple[ServiceKey, ...] = ()
    admin: bool = False
    health: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or self.name != self.name.casefold() or any(c.isspace() for c in self.name):
            raise ValueError("command names must be nonempty, casefolded words")
        if not (inspect.iscoroutinefunction(self.handler) or inspect.iscoroutinefunction(getattr(self.handler, "__call__", None))):
            raise ValueError("command handlers must be async")
        object.__setattr__(self, "needs", tuple(self.needs))
        object.__setattr__(self, "uses", tuple(self.uses))


@dataclass(frozen=True, slots=True)
class Contribution:
    commands: tuple[CommandSpec, ...] = ()
    permission: Any = None
    limiter: Any = None
    help: Any = None

    def __post_init__(self):
        object.__setattr__(self, "commands", tuple(self.commands))


def identity(ctx):
    user = ctx.resolve(USER_ID)
    scene = ctx.resolve(MESSAGE_TYPE)
    if type(user) not in (int, str) or user == "" or not isinstance(scene, str) or not scene:
        return None
    return (ctx.envelope.adapter_id, scene, type(user), user)
