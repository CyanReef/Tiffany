from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any

from .Context import Context
from .Field import Field
from .Hook import Hook
from .Metrics import MetricRegistry
from .Ownership import same_owner
from .Trace import DispatchPhase, DispatchRecord, DispatchResult, TraceRecorder


logger = logging.getLogger(__name__)
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


class Dispatcher:
    """Runs immutable hook routes in stable priority order."""

    __slots__ = (
        "_snapshot",
        "_next_id",
        "_handles",
        "_route_cache",
        "_known_kinds",
        "_known_platforms",
        "_lock",
        "metrics",
        "trace",
        "_on_change",
    )

    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        trace: TraceRecorder | None = None,
    ) -> None:
        self._snapshot = HookSnapshot(0, (), frozenset(), frozenset())
        self._next_id = 1
        # Handles remain queryable after removal so their state methods stay
        # idempotent and introspection can report a removed registration.
        self._handles: dict[int, HookHandle] = {}
        self._route_cache: dict[
            tuple[str | None, str | None],
            _Route,
        ] = {}
        self._known_kinds: frozenset[str] = frozenset()
        self._known_platforms: frozenset[str] = frozenset()
        self._lock = threading.RLock()
        self.metrics = metrics if metrics is not None else MetricRegistry()
        self.trace = trace if trace is not None else TraceRecorder()
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
            handle = HookHandle(self, registration)
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
        self._route_cache.clear()

    def _matching_registrations(
        self,
        platform: str,
        kind: str,
        snapshot: HookSnapshot,
    ) -> tuple[HookRegistration, ...]:
        # Unknown external values share fallback routes, bounding cache keys by
        # the set of registered declarations instead of untrusted input.
        platform_key = platform if platform in snapshot.known_platforms else None
        kind_key = kind if kind in snapshot.known_kinds else None
        cache_key = (platform_key, kind_key)
        current = snapshot is self._snapshot
        if current:
            cached = self._route_cache.get(cache_key)
            if cached is not None and cached.version == snapshot.version:
                return cached.registrations

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
        if current and snapshot is self._snapshot:
            self._route_cache[cache_key] = _Route(snapshot.version, route)
        return route

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
        hook = registration.hook
        phase: DispatchPhase = "predicate" if hook.when is not None else "handler"
        started = perf_counter()
        result: DispatchResult
        error_type: str | None = None
        timeout_scope: asyncio.Timeout | None = None

        async def invoke() -> bool:
            nonlocal phase
            if hook.when is not None:
                phase = "predicate"
                accepted = hook.when(ctx)
                if inspect.isawaitable(accepted):
                    accepted = await accepted
                if not accepted:
                    return False
            phase = "handler"
            await hook.handle(ctx)
            return True

        try:
            if hook.timeout is None:
                accepted = await invoke()
            else:
                timeout_scope = asyncio.timeout(hook.timeout)
                async with timeout_scope:
                    accepted = await invoke()
                if timeout_scope.expired():
                    raise TimeoutError
            if not accepted:
                result = "predicate_rejected"
            elif ctx.stopped:
                result = "stopped"
            else:
                result = "completed"
        except asyncio.CancelledError as error:
            result = "cancelled"
            error_type = type(error).__name__
            self._finish_record(
                ctx,
                registration,
                hook_order,
                registry_version,
                phase,
                result,
                perf_counter() - started,
                queue_depth,
                error_type,
            )
            raise
        except TimeoutError as error:
            if timeout_scope is None or not timeout_scope.expired():
                self._finish_record(
                    ctx,
                    registration,
                    hook_order,
                    registry_version,
                    phase,
                    "error",
                    perf_counter() - started,
                    queue_depth,
                    type(error).__name__,
                )
                if hook.on_error == "continue":
                    logger.exception(
                        "hook %r failed during %s; continuing",
                        hook.name,
                        phase,
                    )
                    return
                raise HookExecutionError(hook.name) from error
            result = "timeout"
            error_type = HookTimeoutError.__name__
            self._finish_record(
                ctx,
                registration,
                hook_order,
                registry_version,
                phase,
                result,
                perf_counter() - started,
                queue_depth,
                error_type,
            )
            policy = hook.on_timeout or hook.on_error
            if policy == "continue":
                logger.warning(
                    "hook %r timed out during %s; continuing",
                    hook.name,
                    phase,
                )
                return
            raise HookTimeoutError(hook.name, hook.timeout or 0.0) from error
        except Exception as error:
            result = "error"
            error_type = type(error).__name__
            self._finish_record(
                ctx,
                registration,
                hook_order,
                registry_version,
                phase,
                result,
                perf_counter() - started,
                queue_depth,
                error_type,
            )
            if hook.on_error == "continue":
                logger.exception(
                    "hook %r failed during %s; continuing",
                    hook.name,
                    phase,
                )
                return
            raise HookExecutionError(hook.name) from error

        self._finish_record(
            ctx,
            registration,
            hook_order,
            registry_version,
            phase,
            result,
            perf_counter() - started,
            queue_depth,
            error_type,
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
        hook = registration.hook
        labels = {
            "platform": ctx.platform,
            "hook": hook.name,
            "reason": result,
        }
        self.metrics.inc("hook_executions_total", labels=labels)
        self.metrics.observe("hook_duration_seconds", duration, labels=labels)
        if not self.trace.enabled:
            return
        self._record_trace(
            ctx,
            registration,
            hook_order,
            registry_version,
            phase,
            result,
            duration,
            queue_depth,
            error_type,
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
        hook = registration.hook
        envelope = ctx.envelope
        event_id = getattr(envelope, "event_id", None)
        connection_id = getattr(envelope, "connection_id", None)
        adapter_id = getattr(envelope, "adapter_id", None)
        if not self.trace.should_record(
            event_id,
            adapter_id=adapter_id,
            connection_id=connection_id,
            registry_version=registry_version,
            hook_id=registration.id,
        ):
            return

        self.trace.record(DispatchRecord(
            event_id=event_id,
            connection_id=connection_id,
            session_id=self.trace.session_reference(
                getattr(envelope, "session_id", None)
            ),
            platform=ctx.platform,
            adapter_id=adapter_id,
            hook_id=registration.id,
            hook_name=hook.name,
            hook_source=registration.source,
            hook_order=hook_order,
            registry_version=registry_version,
            phase=phase,
            result=result,
            duration_seconds=duration,
            queue_depth=queue_depth,
            error_type=error_type,
            error_summary=_error_summary(error_type, phase),
        ))


def _error_summary(
    error_type: str | None,
    phase: DispatchPhase,
) -> str | None:
    """Summarize failures without retaining exception messages or payloads."""

    if error_type is None:
        return None
    return f"{error_type} during {phase}"
