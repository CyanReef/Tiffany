from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal

from .Context import Context
from .Envelope import Envelope
from .EventScheduler import EventScheduler
from .Lifecycle import (
    CleanupResult,
    ComponentStartupTimeoutError,
    DuplicateAdapterIdError,
    LifecycleError,
    RuntimeNotRunningError,
    RuntimeState,
    ShutdownReport,
    StartupResult,
    StopMode,
)
from .Metrics import MetricRegistry
from .Ownership import OwnerKey, same_owner, unique_owners
from .TaskRegistry import TaskInfo, TaskRegistry


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    hooks: Any
    providers: Any
    services: Any


@dataclass(frozen=True, slots=True)
class _LifespanRegistration:
    manager: AbstractAsyncContextManager[Any]
    owner: object


class Lifespan:
    """An explicit async context manager for one Runtime execution."""

    __slots__ = ("runtime",)

    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    async def __aenter__(self) -> "Runtime":
        await self.runtime.setup()
        await self.runtime.start()
        return self.runtime

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        mode: StopMode = "abort" if exc_type is asyncio.CancelledError else "drain"
        try:
            await self.runtime.stop(mode)
        except asyncio.CancelledError:
            task = self.runtime._close_task
            if task is not None:
                await asyncio.shield(task)
            raise
        return False


class Runtime:
    """Lifecycle owner, task supervisor and bounded event scheduler."""

    SETUP_TIMEOUT = 10.0
    START_TIMEOUT = 30.0
    LIFESPAN_TIMEOUT = 30.0
    STARTUP_TIMEOUT = 120.0
    DRAIN_TIMEOUT = 30.0
    ABORT_TIMEOUT = 10.0
    COMPONENT_TIMEOUT = 5.0
    CANCEL_GRACE = 1.0

    __slots__ = (
        "bot",
        "state",
        "tasks",
        "metrics",
        "scheduler",
        "shutdown_report",
        "startup_results",
        "_adapters",
        "_adapter_owners",
        "_lifespans",
        "_setup_components",
        "_setup_ids",
        "_started_services",
        "_started_service_ids",
        "_entered_lifespans",
        "_started_adapters",
        "_started_adapter_ids",
        "_close_task",
        "_close_waiters",
        "_caller_task",
        "_start_lock",
        "_stop_requested",
        "_failed_start",
        "_owner_events",
        "_owner_changed",
        "_service_order",
    )

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.state = RuntimeState.NEW
        self.tasks = TaskRegistry()
        self.tasks.set_failure_handler(self._task_failed)
        self.metrics = MetricRegistry()
        self.scheduler = EventScheduler(self)
        self.shutdown_report: ShutdownReport | None = None
        self.startup_results: list[StartupResult] = []
        self._adapters: list[Any] = []
        self._adapter_owners: list[tuple[Any, object]] = []
        self._lifespans: list[_LifespanRegistration] = []
        self._setup_components: list[Any] = []
        self._setup_ids: set[int] = set()
        self._started_services: list[Any] = []
        self._started_service_ids: set[int] = set()
        self._entered_lifespans: list[_LifespanRegistration] = []
        self._started_adapters: list[Any] = []
        self._started_adapter_ids: set[int] = set()
        self._close_task: asyncio.Task[ShutdownReport] | None = None
        self._close_waiters: set[asyncio.Task[Any]] = set()
        self._caller_task: asyncio.Task[Any] | None = None
        self._start_lock = asyncio.Lock()
        self._stop_requested: StopMode = "drain"
        self._failed_start = False
        self._owner_events: dict[OwnerKey, int] = {}
        self._owner_changed = asyncio.Event()
        self._service_order: list[Any] = []

    @property
    def adapters(self) -> tuple[Any, ...]:
        return tuple(self._adapters)

    def lifespan(self) -> Lifespan:
        return Lifespan(self)

    def install(self, adapter: Any, *, owner: object = "application") -> Any:
        if self.state != RuntimeState.NEW:
            raise LifecycleError("adapters can only be installed before start")
        if not any(item is adapter for item in self._adapters):
            adapter_id = getattr(adapter, "adapter_id", None)
            if adapter_id is not None and any(
                str(getattr(item, "adapter_id", "")) == str(adapter_id)
                for item in self._adapters
            ):
                raise DuplicateAdapterIdError(str(adapter_id))
            self._adapters.append(adapter)
            self._adapter_owners.append((adapter, owner))
        adapter_id = getattr(adapter, "adapter_id", None)
        if adapter_id is not None:
            self.scheduler.configure_adapter(str(adapter_id), 4)
        return adapter

    def add_lifespan(
        self,
        lifespan: AbstractAsyncContextManager[Any],
        *,
        owner: object = "application",
    ) -> None:
        if self.state != RuntimeState.NEW:
            raise LifecycleError("lifespans can only be added before start")
        self._lifespans.append(_LifespanRegistration(lifespan, owner))

    def adapters_for_owner(self, owner: object) -> tuple[Any, ...]:
        return tuple(
            adapter
            for adapter, registered_owner in self._adapter_owners
            if same_owner(registered_owner, owner)
        )

    def lifespans_for_owner(
        self,
        owner: object,
    ) -> tuple[_LifespanRegistration, ...]:
        return tuple(
            registration
            for registration in self._lifespans
            if same_owner(registration.owner, owner)
        )

    async def setup(self, runtime: object | None = None) -> None:
        if self._caller_task is None:
            self._caller_task = asyncio.current_task()
        async with self._start_lock:
            if self.state == RuntimeState.SETUP:
                return
            if self.state != RuntimeState.NEW or self._failed_start:
                raise LifecycleError(f"runtime cannot setup from {self.state.value!r}")
            self.state = RuntimeState.SETTING_UP
            try:
                services = [handle.component for handle in self.bot.services.lifecycle_order()]
                self._service_order = services
                components: list[Any] = []
                component_ids: set[int] = set()
                for component in (*services, *self._adapters):
                    if id(component) in component_ids:
                        continue
                    component_ids.add(id(component))
                    components.append(component)
                async with asyncio.timeout(self.STARTUP_TIMEOUT):
                    for component in components:
                        await self._call_startup(
                            component,
                            "setup",
                            self.SETUP_TIMEOUT,
                            self,
                        )
                        self._setup_components.append(component)
                        self._setup_ids.add(id(component))
                # Adapter setup may install protocol-owned synchronous
                # providers. Freeze and validate only after every component
                # has declared its capabilities, before any I/O starts.
                self.bot.services.validate()
                self.bot._validate_registrations()
                self.state = RuntimeState.SETUP
            except BaseException:
                self._failed_start = True
                await self._rollback_startup()
                raise

    async def start(self) -> None:
        if self.state == RuntimeState.NEW:
            await self.setup()
        async with self._start_lock:
            if self.state == RuntimeState.RUNNING:
                return
            if self.state != RuntimeState.SETUP or self._failed_start:
                raise LifecycleError(f"runtime cannot start from {self.state.value!r}")
            self.state = RuntimeState.STARTING
            try:
                self.bot.services.validate()
                self.bot._validate_registrations()
                async with asyncio.timeout(self.STARTUP_TIMEOUT):
                    for component in self._service_order:
                        if id(component) in self._started_service_ids:
                            continue
                        await self._call_startup(
                            component,
                            "start",
                            self.START_TIMEOUT,
                        )
                        self._started_services.append(component)
                        self._started_service_ids.add(id(component))
                    for registration in self._lifespans:
                        await self._call_startup(
                            registration.manager,
                            "__aenter__",
                            self.LIFESPAN_TIMEOUT,
                        )
                        self._entered_lifespans.append(registration)
                    for adapter in self._adapters:
                        if id(adapter) in self._started_adapter_ids:
                            continue
                        await self._call_startup(
                            adapter,
                            "start",
                            self.START_TIMEOUT,
                        )
                        self._started_adapters.append(adapter)
                        self._started_adapter_ids.add(id(adapter))
                self.scheduler.start()
                self.state = RuntimeState.RUNNING
            except BaseException:
                self._failed_start = True
                await self._rollback_startup()
                raise

    async def emit(
        self,
        envelope: Envelope,
        *,
        reject: bool = True,
        wait: bool = True,
    ) -> Context | asyncio.Future[Context] | None:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeNotRunningError(self.state)
        if not self.bot._is_validated(envelope.platform):
            self.bot.validate(envelope.platform)
        snapshot = RuntimeSnapshot(
            hooks=self.bot.dispatcher.snapshot(),
            providers=self.bot.providers.snapshot(),
            services=self.bot.services.snapshot(),
        )
        owners = unique_owners(
            registration.owner
            for registration in snapshot.hooks.registrations
            if registration.enabled
        )
        return await self.scheduler.submit(
            envelope,
            adapter_id=envelope.adapter_id,
            reject=reject,
            snapshot=snapshot,
            wait=wait,
            owners=owners,
        )

    def _retain_event_owners(self, owners: tuple[object, ...]) -> None:
        for owner in owners:
            key = OwnerKey(owner)
            self._owner_events[key] = self._owner_events.get(key, 0) + 1

    def _release_event_owners(self, owners: tuple[object, ...]) -> None:
        changed = False
        for owner in owners:
            key = OwnerKey(owner)
            count = self._owner_events.get(key, 0) - 1
            if count > 0:
                self._owner_events[key] = count
            else:
                self._owner_events.pop(key, None)
            changed = True
        if changed:
            self._owner_changed.set()

    async def wait_owner_events(self, owner: object, timeout: float) -> bool:
        key = OwnerKey(owner)
        try:
            async with asyncio.timeout(timeout):
                while self._owner_events.get(key, 0):
                    self._owner_changed.clear()
                    if not self._owner_events.get(key, 0):
                        return True
                    await self._owner_changed.wait()
            return True
        except TimeoutError:
            return False

    async def _dispatch(
        self,
        envelope: Envelope,
        queue_depth: int,
        snapshot: RuntimeSnapshot | None,
    ) -> Context:
        if snapshot is None:
            snapshot = RuntimeSnapshot(
                hooks=self.bot.dispatcher.snapshot(),
                providers=self.bot.providers.snapshot(),
                services=self.bot.services.snapshot(),
            )
        ctx = Context(
            envelope,
            self.bot.providers,
            self.bot.services,
            provider_snapshot=snapshot.providers,
            service_snapshot=snapshot.services,
        )
        started = time.perf_counter()
        try:
            await self.bot.dispatcher.dispatch(
                ctx,
                snapshot=snapshot.hooks,
                queue_depth=queue_depth,
            )
            return ctx
        finally:
            self.metrics.observe(
                "event_duration_seconds",
                time.perf_counter() - started,
                labels={
                    "platform": envelope.platform,
                    "adapter": envelope.adapter_id,
                },
            )

    async def stop(self, mode: StopMode = "drain") -> ShutdownReport:
        if mode not in ("drain", "abort"):
            raise ValueError(f"unknown stop mode: {mode!r}")
        if self._caller_task is None:
            self._caller_task = asyncio.current_task()
        if mode == "abort":
            self._stop_requested = "abort"
            if self.state == RuntimeState.STOPPING_DRAIN:
                self.scheduler.request_abort()
        if self.state in (RuntimeState.TERMINATED, RuntimeState.STOP_FAILED):
            return self.shutdown_report or ShutdownReport()
        task = self._close_task
        if task is None:
            task = self.tasks.spawn(
                self._close(),
                name="runtime:close",
                owner="runtime-close",
                critical=False,
            )
            self._close_task = task
        waiter = asyncio.current_task()
        if waiter is not None:
            self._close_waiters.add(waiter)
        try:
            return await asyncio.shield(task)
        finally:
            if waiter is not None:
                self._close_waiters.discard(waiter)

    close = stop

    async def teardown(self) -> ShutdownReport:
        return await self.stop(self._stop_requested)

    async def _close(self) -> ShutdownReport:
        if self.state in (RuntimeState.TERMINATED, RuntimeState.STOP_FAILED):
            return self.shutdown_report or ShutdownReport()
        report = ShutdownReport(startup_results=tuple(self.startup_results))
        shutdown_started = time.perf_counter()
        self.shutdown_report = report
        self.scheduler.stop_admission()
        drain_started = time.perf_counter()
        running = self.state == RuntimeState.RUNNING
        if self._stop_requested == "drain" and running:
            self.state = RuntimeState.STOPPING_DRAIN
            try:
                async with asyncio.timeout(self.DRAIN_TIMEOUT):
                    events_drained = await self.scheduler.drain(self.DRAIN_TIMEOUT)
                    adapters_drained = True
                    if events_drained:
                        for component in reversed(self._started_adapters):
                            if self._stop_requested == "abort":
                                adapters_drained = False
                                break
                            component_deadline = (
                                asyncio.get_running_loop().time()
                                + self.COMPONENT_TIMEOUT
                            )
                            adapters_drained = (
                                await self._cleanup(
                                    component,
                                    "stop",
                                    "drain",
                                    report,
                                    component_deadline,
                                )
                                and adapters_drained
                            )
                    drained = events_drained and adapters_drained
            except TimeoutError:
                drained = False
            report.drain_duration = time.perf_counter() - drain_started
            self.metrics.observe("runtime_drain_seconds", report.drain_duration)
            if not drained:
                self._stop_requested = "abort"
        elif not running:
            self._stop_requested = "abort"
        if self._stop_requested == "abort":
            report.forced = True
            self.metrics.inc("runtime_forced_shutdown_total")
            self.state = RuntimeState.STOPPING_ABORT

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ABORT_TIMEOUT
        if report.forced:
            abandoned = await self.scheduler.abort(self.CANCEL_GRACE)
            report.abandoned_tasks = abandoned

        for component in reversed(self._started_adapters):
            if report.forced:
                await self._cleanup(component, "stop", "abort", report, deadline)
        self._started_adapters.clear()
        self._started_adapter_ids.clear()

        self.state = RuntimeState.STOPPED
        self.state = RuntimeState.TEARING_DOWN
        for registration in reversed(self._entered_lifespans):
            await self._cleanup(
                registration.manager,
                "__aexit__",
                None,
                report,
                deadline,
            )
        self._entered_lifespans.clear()
        for component in reversed(self._started_services):
            await self._cleanup(component, "stop", self._stop_requested, report, deadline)
        self._started_services.clear()
        self._started_service_ids.clear()
        for component in reversed(self._setup_components):
            await self._cleanup(component, "teardown", None, report, deadline)
        self._setup_components.clear()
        self._setup_ids.clear()

        abandoned = await self.tasks.close(self.CANCEL_GRACE)
        if abandoned:
            report.abandoned_tasks = tuple(dict.fromkeys(
                (*report.abandoned_tasks, *abandoned)
            ))
        if self.scheduler.queued or self.scheduler.active or self._owner_events:
            residue = RuntimeError(
                "runtime shutdown left event queues or owner references"
            )
            report.failures.append(residue)
        self._record_residual_tasks(report)
        self.state = (
            RuntimeState.TERMINATED if report.successful else RuntimeState.STOP_FAILED
        )
        self.metrics.observe(
            "runtime_shutdown_seconds",
            time.perf_counter() - shutdown_started,
            labels={"reason": "forced" if report.forced else "drained"},
        )
        return report

    async def _rollback_startup(self) -> None:
        report = ShutdownReport(forced=True)
        report.startup_results = tuple(self.startup_results)
        self.shutdown_report = report
        deadline = asyncio.get_running_loop().time() + self.ABORT_TIMEOUT
        self.scheduler.stop_admission()
        report.abandoned_tasks = await self.scheduler.abort(self.CANCEL_GRACE)
        for component in reversed(self._started_adapters):
            await self._cleanup(component, "stop", "abort", report, deadline)
        for registration in reversed(self._entered_lifespans):
            await self._cleanup(
                registration.manager,
                "__aexit__",
                None,
                report,
                deadline,
            )
        for component in reversed(self._started_services):
            await self._cleanup(component, "stop", "abort", report, deadline)
        for component in reversed(self._setup_components):
            await self._cleanup(component, "teardown", None, report, deadline)
        self._started_adapters.clear()
        self._started_adapter_ids.clear()
        self._entered_lifespans.clear()
        self._started_services.clear()
        self._started_service_ids.clear()
        self._setup_components.clear()
        self._setup_ids.clear()
        abandoned = await self.tasks.close(self.CANCEL_GRACE)
        if abandoned:
            report.abandoned_tasks = abandoned
        self._record_residual_tasks(report)
        self.state = RuntimeState.STOP_FAILED

    async def _call_startup(
        self,
        component: Any,
        method_name: str,
        timeout: float,
        *args: Any,
    ) -> None:
        method = getattr(component, method_name, None)
        if method is None:
            return
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                result = method(*args)
                if inspect.isawaitable(result):
                    await result
        except TimeoutError as error:
            self.startup_results.append(StartupResult(
                component=_component_name(component),
                phase=method_name,
                outcome="timeout",
                duration=time.perf_counter() - started,
                error_type=type(error).__name__,
                error_message=f"{method_name} exceeded {timeout:g} seconds",
            ))
            raise ComponentStartupTimeoutError(
                _component_name(component), method_name, timeout
            ) from error
        except BaseException as error:
            self.startup_results.append(StartupResult(
                component=_component_name(component),
                phase=method_name,
                outcome="error",
                duration=time.perf_counter() - started,
                error_type=type(error).__name__,
                error_message=_redact_error(error),
            ))
            raise
        else:
            self.startup_results.append(StartupResult(
                component=_component_name(component),
                phase=method_name,
                outcome="completed",
                duration=time.perf_counter() - started,
            ))

    async def _cleanup(
        self,
        component: Any,
        phase: str,
        argument: Any,
        report: ShutdownReport,
        deadline: float,
    ) -> bool:
        method = getattr(component, phase, None)
        if method is None:
            return True
        started = time.perf_counter()
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        timeout = min(self.COMPONENT_TIMEOUT, remaining)
        outcome: Literal["completed", "error", "timeout", "abandoned"] = "completed"
        error_type = error_message = None
        try:
            if timeout <= 0:
                raise TimeoutError
            async def invoke_cleanup() -> None:
                if phase == "__aexit__":
                    result = method(None, None, None)
                elif argument is None:
                    result = method()
                else:
                    result = method(argument)
                if inspect.isawaitable(result):
                    await result

            task = self.tasks.spawn(
                invoke_cleanup(),
                name=f"cleanup:{_component_name(component)}:{phase}",
                owner="runtime-cleanup",
                critical=False,
            )
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if task not in done:
                task.cancel()
                _, pending = await asyncio.wait(
                    (task,),
                    timeout=min(self.CANCEL_GRACE, max(
                        0.0,
                        deadline - asyncio.get_running_loop().time(),
                    )),
                )
                if pending:
                    outcome = "abandoned"
                    report.abandoned_tasks = tuple(dict.fromkeys((
                        *report.abandoned_tasks,
                        task.get_name(),
                    )))
                    report.abandoned_components = tuple(dict.fromkeys((
                        *report.abandoned_components,
                        _component_name(component),
                    )))
                raise TimeoutError
            await task
        except TimeoutError as error:
            if outcome != "abandoned":
                outcome = "timeout"
            error_type = type(error).__name__
            error_message = f"{phase} exceeded cleanup deadline"
            report.failures.append(error)
        except BaseException as error:
            outcome = "error"
            error_type = type(error).__name__
            error_message = _redact_error(error)
            report.failures.append(error)
        report.results.append(CleanupResult(
            component=_component_name(component),
            phase=phase,
            outcome=outcome,
            duration=time.perf_counter() - started,
            error_type=error_type,
            error_message=error_message,
        ))
        return outcome == "completed"

    def _record_residual_tasks(self, report: ShutdownReport) -> None:
        current = asyncio.current_task()
        managed = set(self.tasks._tasks)
        close_waiters = set(self._close_waiters)
        if self._caller_task is not None:
            close_waiters.add(self._caller_task)
        unmanaged = tuple(
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not current
            and not task.done()
            and task not in managed
            and task not in close_waiters
        )
        if unmanaged:
            report.abandoned_tasks = tuple(dict.fromkeys((
                *report.abandoned_tasks,
                *unmanaged,
            )))

    def _task_failed(self, info: TaskInfo, error: BaseException) -> None:
        if info.failure_policy == "disable_owner":
            disable = getattr(self.bot, "unload", None)
            if disable is not None:
                self.tasks.spawn(
                    disable(info.owner, mode="abort"),
                    name=f"disable-owner:{info.name}",
                    owner="runtime",
                    critical=False,
                )
            return
        if self.state in (
            RuntimeState.RUNNING,
            RuntimeState.STARTING,
            RuntimeState.SETUP,
        ):
            self._stop_requested = "abort"
            if self._close_task is None:
                self._close_task = self.tasks.spawn(
                    self._close(),
                    name="runtime:critical-abort",
                    owner="runtime-close",
                    critical=False,
                )

    async def __aenter__(self) -> "Runtime":
        self._caller_task = asyncio.current_task()
        return await self.lifespan().__aenter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        try:
            return await self.lifespan().__aexit__(exc_type, exc, tb)
        finally:
            self._caller_task = None


def _component_name(component: Any) -> str:
    return getattr(component, "name", None) or type(component).__name__


def _redact_error(error: BaseException) -> str:
    text = str(error).replace("\n", " ")
    return text[:256]
