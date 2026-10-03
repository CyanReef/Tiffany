"""Copy-on-write hook registration and dependency declarations."""
from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from ..Field import Field
from ..Hook import Hook
from ..Ownership import same_owner
from .models import HookHandle, HookRegistration, HookSnapshot, _UNSET

if TYPE_CHECKING:
    from ..Dispatcher import Dispatcher


class HookRegistry:
    __slots__ = (
        "_dispatcher", "_snapshot", "_next_id", "_handles",
        "_known_kinds", "_known_platforms", "_lock", "_on_change",
    )

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._snapshot = HookSnapshot(0, (), frozenset(), frozenset())
        self._next_id = 1
        # Keep handles queryable after removal for idempotent control methods.
        self._handles: dict[int, HookHandle] = {}
        self._known_kinds: frozenset[str] = frozenset()
        self._known_platforms: frozenset[str] = frozenset()
        self._lock = threading.RLock()
        self._on_change: Any = None

    @property
    def version(self) -> int:
        return self._snapshot.version

    @property
    def revision(self) -> int:
        return self.version

    @property
    def hooks(self) -> tuple[Hook, ...]:
        return self._snapshot.hooks

    def snapshot(self) -> HookSnapshot:
        """Return the current immutable registration view in O(1)."""

        return self._snapshot

    def required_fields(
        self,
        platform: str,
        snapshot: HookSnapshot | None = None,
    ) -> tuple[Field[Any], ...]:
        view = self._snapshot if snapshot is None else snapshot
        fields: dict[Field[Any], None] = {}
        for registration in view.registrations:
            hook = registration.hook
            if registration.enabled and (
                hook.platform is None or hook.platform == platform
            ):
                fields.update(dict.fromkeys(hook.needs))
        return tuple(fields)

    def required_services(
        self,
        platform: str,
        snapshot: HookSnapshot | None = None,
    ) -> tuple[Any, ...]:
        view = self._snapshot if snapshot is None else snapshot
        services: dict[Any, None] = {}
        for registration in view.registrations:
            hook = registration.hook
            if registration.enabled and (
                hook.platform is None or hook.platform == platform
            ):
                services.update(dict.fromkeys(hook.uses))
        return tuple(services)

    def platforms(self) -> set[str]:
        return {
            registration.hook.platform
            for registration in self._snapshot.registrations
            if registration.enabled and registration.hook.platform is not None
        }

    def add(
        self,
        hook: Hook,
        *,
        source: str | None = None,
        owner: object = None,
    ) -> HookHandle:
        """Register a hook and return its independent lifecycle handle."""

        if not isinstance(hook, Hook):
            raise TypeError("dispatcher.add expects a Hook")
        if source is None:
            source = hook.source or getattr(hook.handle, "__module__", None)
        if owner is None:
            owner = hook.owner

        with self._lock:
            registration = HookRegistration(
                id=self._next_id,
                hook=hook,
                source=source,
                owner=owner,
            )
            self._next_id += 1
            registrations = list(self._snapshot.registrations)
            # Registration is cold; insert once so dispatch never sorts. Equal
            # priorities remain in registration order.
            index = len(registrations)
            while (
                index > 0
                and registrations[index - 1].hook.priority < hook.priority
            ):
                index -= 1
            registrations.insert(index, registration)
            handle = HookHandle(self._dispatcher, registration)
            self._handles[registration.id] = handle
            self._publish(tuple(registrations))
            if self._on_change is not None:
                self._on_change()
            return handle

    def handle_for(self, func: Any) -> HookHandle | None:
        """Return the most recently registered live handle for a callable."""

        best_id = -1
        result: HookHandle | None = None
        for registration in self._snapshot.registrations:
            if registration.hook.handle is func and registration.id > best_id:
                best_id = registration.id
                result = self._handles.get(registration.id)
        return result

    def registrations(
        self,
        *,
        source: str | None | object = _UNSET,
        owner: object = _UNSET,
    ) -> tuple[HookHandle, ...]:
        """Query live handles in effective execution order."""

        return tuple(
            self._handles[registration.id]
            for registration in self._snapshot.registrations
            if (source is _UNSET or registration.source == source)
            and (owner is _UNSET or same_owner(registration.owner, owner))
        )

    def handles(self) -> tuple[HookHandle, ...]:
        return self.registrations()

    def handles_for_source(self, source: str | None) -> tuple[HookHandle, ...]:
        return self.registrations(source=source)

    def for_source(self, source: str | None) -> tuple[HookHandle, ...]:
        return self.handles_for_source(source)

    def handles_for_owner(self, owner: object) -> tuple[HookHandle, ...]:
        return self.registrations(owner=owner)

    def for_owner(self, owner: object) -> tuple[HookHandle, ...]:
        return self.handles_for_owner(owner)

    def remove_owner(self, owner: object) -> int:
        """Atomically remove all hooks belonging to an owner."""

        with self._lock:
            ids = {
                registration.id
                for registration in self._snapshot.registrations
                if same_owner(registration.owner, owner)
            }
            removed = self._remove_ids_locked(ids)
        if removed and self._on_change is not None:
            self._on_change()
        return removed

    def _contains(self, registration_id: int) -> bool:
        return self._registration(registration_id) is not None

    def _registration(self, registration_id: int) -> HookRegistration | None:
        for registration in self._snapshot.registrations:
            if registration.id == registration_id:
                return registration
        return None

    def _set_enabled(self, registration_id: int, enabled: bool) -> bool:
        with self._lock:
            registrations = self._snapshot.registrations
            for index, registration in enumerate(registrations):
                if registration.id != registration_id:
                    continue
                if registration.enabled == enabled:
                    return False
                updated = (
                    registrations[:index]
                    + (replace(registration, enabled=enabled),)
                    + registrations[index + 1:]
                )
                self._publish(updated)
                return True
            return False

    def _remove(self, registration_id: int) -> bool:
        with self._lock:
            return bool(self._remove_ids_locked({registration_id}))

    def _remove_ids_locked(self, registration_ids: set[int]) -> int:
        if not registration_ids:
            return 0
        current = self._snapshot.registrations
        updated = tuple(
            registration
            for registration in current
            if registration.id not in registration_ids
        )
        removed = len(current) - len(updated)
        if not removed:
            return 0
        self._publish(updated)
        return removed

    def _publish(self, registrations: tuple[HookRegistration, ...]) -> None:
        known_kinds = frozenset(
            registration.hook.on
            for registration in registrations
            if registration.enabled and registration.hook.on is not None
        )
        known_platforms = frozenset(
            registration.hook.platform
            for registration in registrations
            if registration.enabled and registration.hook.platform is not None
        )
        self._snapshot = HookSnapshot(
            self._snapshot.version + 1,
            registrations,
            known_kinds,
            known_platforms,
        )
        self._known_kinds = known_kinds
        self._known_platforms = known_platforms
        self._dispatcher._route_cache.clear()
