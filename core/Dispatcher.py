"""Public dispatcher facade: registration, route selection and hook execution."""
from __future__ import annotations

from typing import Any

from .Context import Context
from .Field import Field
from .Hook import Hook
from .Metrics import MetricRegistry
from .Trace import DispatchPhase, DispatchResult, TraceRecorder
from .dispatch.execution import HookExecutor
from .dispatch.models import (
    HookExecutionError, HookHandle, HookRegistration, HookSnapshot, HookTimeoutError,
    _Route, _UNSET,
)
from .dispatch.registry import HookRegistry
from .dispatch.routing import HookRouter


class Dispatcher:
    """Runs immutable hook routes in stable priority order."""

    __slots__ = ("_registry", "_router", "_executor")

    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        trace: TraceRecorder | None = None,
    ) -> None:
        self._registry = HookRegistry(self)
        self._router = HookRouter(self._registry)
        self._executor = HookExecutor(metrics, trace)

    @property
    def metrics(self) -> MetricRegistry:
        return self._executor.metrics

    @metrics.setter
    def metrics(self, value: MetricRegistry) -> None:
        self._executor.metrics = value

    @property
    def trace(self) -> TraceRecorder:
        return self._executor.trace

    @trace.setter
    def trace(self, value: TraceRecorder) -> None:
        self._executor.trace = value

    @property
    def _snapshot(self) -> HookSnapshot:
        return self._registry.snapshot()

    @property
    def _route_cache(self) -> dict[tuple[str | None, str | None], _Route]:
        return self._router._route_cache

    @property
    def _on_change(self) -> Any:
        return self._registry._on_change

    @_on_change.setter
    def _on_change(self, value: Any) -> None:
        self._registry._on_change = value

    @property
    def version(self) -> int:
        return self._registry.version

    @property
    def revision(self) -> int:
        return self._registry.revision

    @property
    def hooks(self) -> tuple[Hook, ...]:
        return self._registry.hooks

    def snapshot(self) -> HookSnapshot:
        """Return the current immutable registration view in O(1)."""

        return self._registry.snapshot()

    def required_fields(
        self,
        platform: str,
        snapshot: HookSnapshot | None = None,
    ) -> tuple[Field[Any], ...]:
        return self._registry.required_fields(platform, snapshot)

    def required_services(
        self,
        platform: str,
        snapshot: HookSnapshot | None = None,
    ) -> tuple[Any, ...]:
        return self._registry.required_services(platform, snapshot)

    def platforms(self) -> set[str]:
        return self._registry.platforms()

    def add(
        self,
        hook: Hook,
        *,
        source: str | None = None,
        owner: object = None,
    ) -> HookHandle:
        """Register a hook and return its independent lifecycle handle."""

        return self._registry.add(hook, source=source, owner=owner)

    def handle_for(self, func: Any) -> HookHandle | None:
        """Return the most recently registered live handle for a callable."""

        return self._registry.handle_for(func)

    def registrations(
        self,
        *,
        source: str | None | object = _UNSET,
        owner: object = _UNSET,
    ) -> tuple[HookHandle, ...]:
        """Query live handles in effective execution order."""

        return self._registry.registrations(source=source, owner=owner)

    def handles(self) -> tuple[HookHandle, ...]:
        return self._registry.handles()

    def handles_for_source(self, source: str | None) -> tuple[HookHandle, ...]:
        return self._registry.handles_for_source(source)

    def for_source(self, source: str | None) -> tuple[HookHandle, ...]:
        return self._registry.for_source(source)

    def handles_for_owner(self, owner: object) -> tuple[HookHandle, ...]:
        return self._registry.handles_for_owner(owner)

    def for_owner(self, owner: object) -> tuple[HookHandle, ...]:
        return self._registry.for_owner(owner)

    def remove_owner(self, owner: object) -> int:
        """Atomically remove all hooks belonging to an owner."""

        return self._registry.remove_owner(owner)

    def _contains(self, registration_id: int) -> bool:
        return self._registry._contains(registration_id)

    def _registration(self, registration_id: int) -> HookRegistration | None:
        return self._registry._registration(registration_id)

    def _set_enabled(self, registration_id: int, enabled: bool) -> bool:
        return self._registry._set_enabled(registration_id, enabled)

    def _remove(self, registration_id: int) -> bool:
        return self._registry._remove(registration_id)

    def _remove_ids_locked(self, registration_ids: set[int]) -> int:
        return self._registry._remove_ids_locked(registration_ids)

    def _publish(self, registrations: tuple[HookRegistration, ...]) -> None:
        return self._registry._publish(registrations)

    def _matching_registrations(
        self,
        platform: str,
        kind: str,
        snapshot: HookSnapshot,
    ) -> tuple[HookRegistration, ...]:
        return self._router._matching_registrations(platform, kind, snapshot)

    def _matching_route(
        self,
        platform: str,
        kind: str,
        snapshot: HookSnapshot,
    ) -> _Route:
        # Unknown external values share fallback routes, bounding cache keys by
        # the set of registered declarations instead of untrusted input.
        return self._router._matching_route(platform, kind, snapshot)

    def _matching_hooks(self, platform: str, kind: str) -> tuple[Hook, ...]:
        """Compatibility view used by diagnostics and older integrations."""

        return self._router._matching_hooks(platform, kind)

    def _owners_for(
        self, platform: str, kind: str, snapshot: HookSnapshot
    ) -> tuple[object, ...]:
        """Owners of the hooks captured by this event's dispatch route."""

        return self._router._owners_for(platform, kind, snapshot)

    async def dispatch(
        self,
        ctx: Context,
        *,
        queue_depth: int | None = None,
        snapshot: HookSnapshot | None = None,
    ) -> None:
        # Capture exactly one registry view. Handle mutations performed by a
        # running hook only affect subsequent events.
        view = self._snapshot if snapshot is None else snapshot
        route = self._matching_registrations(ctx.platform, ctx.kind, view)
        for hook_order, registration in enumerate(route):
            if ctx.stopped:
                if not self.trace.enabled:
                    break
                hook = registration.hook
                phase: DispatchPhase = (
                    "predicate" if hook.when is not None else "handler"
                )
                self._record_trace(
                    ctx,
                    registration,
                    hook_order,
                    view.version,
                    phase,
                    "skipped",
                    0.0,
                    queue_depth,
                    None,
                )
                continue
            await self._execute(
                ctx,
                registration,
                hook_order,
                view.version,
                queue_depth,
            )

    async def _execute(
        self,
        ctx: Context,
        registration: HookRegistration,
        hook_order: int,
        registry_version: int,
        queue_depth: int | None,
    ) -> None:
        return await self._executor._execute(
            ctx, registration, hook_order, registry_version, queue_depth,
        )

    def _finish_record(
        self,
        ctx: Context,
        registration: HookRegistration,
        hook_order: int,
        registry_version: int,
        phase: DispatchPhase,
        result: DispatchResult,
        duration: float,
        queue_depth: int | None,
        error_type: str | None,
    ) -> None:
        return self._executor._finish_record(
            ctx, registration, hook_order, registry_version, phase, result,
            duration, queue_depth, error_type,
        )

    def _record_trace(
        self,
        ctx: Context,
        registration: HookRegistration,
        hook_order: int,
        registry_version: int,
        phase: DispatchPhase,
        result: DispatchResult,
        duration: float,
        queue_depth: int | None,
        error_type: str | None,
    ) -> None:
        return self._executor._record_trace(
            ctx, registration, hook_order, registry_version, phase, result,
            duration, queue_depth, error_type,
        )
