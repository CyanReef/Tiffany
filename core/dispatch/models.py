"""Immutable hook declarations, snapshots, control handles and execution errors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..Hook import Hook

if TYPE_CHECKING:
    from ..Dispatcher import Dispatcher

_UNSET = object()


class HookExecutionError(RuntimeError):
    def __init__(self, hook_name: str):
        super().__init__(f"hook {hook_name!r} failed")
        self.hook_name = hook_name


class HookTimeoutError(HookExecutionError):
    """Raised when an aborting hook exceeds its declared timeout."""

    def __init__(self, hook_name: str, timeout: float):
        super().__init__(hook_name)
        self.timeout = timeout


@dataclass(frozen=True, slots=True)
class HookRegistration:
    """Immutable declaration state held by a dispatcher snapshot."""

    id: int
    hook: Hook
    source: str | None
    owner: object
    enabled: bool = True

    @property
    def name(self) -> str:
        return self.hook.name


@dataclass(frozen=True, slots=True)
class HookSnapshot:
    """A stable copy-on-write registry view."""

    version: int
    registrations: tuple[HookRegistration, ...]
    known_kinds: frozenset[str]
    known_platforms: frozenset[str]

    @property
    def revision(self) -> int:
        return self.version

    @property
    def hooks(self) -> tuple[Hook, ...]:
        return tuple(
            registration.hook
            for registration in self.registrations
            if registration.enabled
        )


class HookHandle:
    """A stable control handle for one hook registration."""

    __slots__ = ("_dispatcher", "_registration")

    def __init__(
        self,
        dispatcher: "Dispatcher",
        registration: HookRegistration,
    ) -> None:
        self._dispatcher = dispatcher
        self._registration = registration

    def _changed(self) -> None:
        callback = self._dispatcher._on_change
        if callback is not None:
            callback()

    @property
    def id(self) -> int:
        return self._registration.id

    @property
    def name(self) -> str:
        return self._registration.hook.name

    @property
    def hook(self) -> Hook:
        return self._registration.hook

    @property
    def source(self) -> str | None:
        return self._registration.source

    @property
    def owner(self) -> object:
        return self._registration.owner

    @property
    def active(self) -> bool:
        """Whether this registration has not been removed."""

        return self._dispatcher._contains(self.id)

    @property
    def enabled(self) -> bool:
        registration = self._dispatcher._registration(self.id)
        return registration is not None and registration.enabled

    @property
    def removed(self) -> bool:
        return not self.active

    def enable(self) -> bool:
        changed = self._dispatcher._set_enabled(self.id, True)
        if changed:
            self._changed()
        return changed

    def disable(self) -> bool:
        changed = self._dispatcher._set_enabled(self.id, False)
        if changed:
            self._changed()
        return changed

    def remove(self) -> bool:
        changed = self._dispatcher._remove(self.id)
        if changed:
            self._changed()
        return changed

    def __repr__(self) -> str:
        if not self.active:
            state = "removed"
        elif self.enabled:
            state = "enabled"
        else:
            state = "disabled"
        return (
            f"HookHandle(id={self.id}, name={self.name!r}, "
            f"source={self.source!r}, state={state!r})"
        )


@dataclass(frozen=True, slots=True)
class _Route:
    version: int
    registrations: tuple[HookRegistration, ...]
    owners: tuple[object, ...]
