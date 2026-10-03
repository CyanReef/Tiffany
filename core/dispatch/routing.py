"""Bounded current-snapshot routes and execution of historical snapshots."""
from __future__ import annotations

from ..Hook import Hook
from ..Ownership import unique_owners
from .models import HookRegistration, HookSnapshot, _Route
from .registry import HookRegistry


class HookRouter:
    __slots__ = ("_registry", "_route_cache")

    def __init__(self, registry: HookRegistry) -> None:
        self._registry = registry
        self._route_cache: dict[tuple[str | None, str | None], _Route] = {}

    @property
    def _snapshot(self) -> HookSnapshot:
        return self._registry.snapshot()

    def _matching_registrations(
        self,
        platform: str,
        kind: str,
        snapshot: HookSnapshot,
    ) -> tuple[HookRegistration, ...]:
        return self._matching_route(platform, kind, snapshot).registrations

    def _matching_route(
        self,
        platform: str,
        kind: str,
        snapshot: HookSnapshot,
    ) -> _Route:
        # Unknown external values share fallback routes, bounding cache keys by
        # the set of registered declarations instead of untrusted input.
        platform_key = platform if platform in snapshot.known_platforms else None
        kind_key = kind if kind in snapshot.known_kinds else None
        cache_key = (platform_key, kind_key)
        current = snapshot is self._snapshot
        if current:
            cached = self._route_cache.get(cache_key)
            if cached is not None and cached.version == snapshot.version:
                return cached

        route = tuple(
            registration
            for registration in snapshot.registrations
            if registration.enabled
            and (
                registration.hook.platform is None
                or registration.hook.platform == platform_key
            )
            and (
                registration.hook.on is None
                or registration.hook.on == kind_key
            )
        )
        # Historical snapshots stay executable but do not retain obsolete
        # route keys. Registry mutations are cold, so recomputing an old route
        # is preferable to allowing in-flight versions to grow the cache.
        result = _Route(
            snapshot.version,
            route,
            unique_owners(registration.owner for registration in route),
        )
        if current and snapshot is self._snapshot:
            self._route_cache[cache_key] = result
        return result

    def _matching_hooks(self, platform: str, kind: str) -> tuple[Hook, ...]:
        """Compatibility view used by diagnostics and older integrations."""

        snapshot = self._snapshot
        return tuple(
            registration.hook
            for registration in self._matching_registrations(
                platform,
                kind,
                snapshot,
            )
        )

    def _owners_for(
        self, platform: str, kind: str, snapshot: HookSnapshot
    ) -> tuple[object, ...]:
        """Owners of the hooks captured by this event's dispatch route."""

        return self._matching_route(platform, kind, snapshot).owners
