from __future__ import annotations

import asyncio
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from .Context import Context
from .Envelope import Envelope
from .EventScheduler import EventScheduler
from .Lifecycle import (
    DuplicateAdapterIdError,
    LifecycleError,
    RuntimeNotRunningError,
    RuntimeState,
    ShutdownReport,
    StartupResult,
    StopMode,
)
from .Metrics import MetricRegistry
from .Ownership import OwnerKey, same_owner
from .TaskRegistry import TaskInfo, TaskRegistry
from .runtime import components, owners as owner_resources, shutdown, startup


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
        "_start_lock",
        "_stop_requested",
        "_failed_start",
        "_owner_events",
        "_owner_changed",
        "_service_order",
        "_closed",
        "failure_cause",
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
        self._start_lock = asyncio.Lock()
        self._stop_requested: StopMode = "drain"
        self._failed_start = False
        self._owner_events: dict[OwnerKey, int] = {}
        self._owner_changed = asyncio.Event()
        self._service_order: list[Any] = []
        self._closed = asyncio.Event()
        self.failure_cause: BaseException | None = None

    async def wait_closed(self) -> ShutdownReport:
        """Wait for cleanup (including startup rollback) to finish."""
        await self._closed.wait()
        return self.shutdown_report or ShutdownReport()

    @property
    def startup_failed(self) -> bool:
        """Whether setup or start failed and entered rollback."""
        return self._failed_start

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
        return await startup.setup(self)

    async def start(self) -> None:
        return await startup.start(self)

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
        owners = self.bot.dispatcher._owners_for(
            envelope.platform, envelope.kind, snapshot.hooks
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
        return owner_resources._retain_event_owners(self, owners)

    def _release_event_owners(self, owners: tuple[object, ...]) -> None:
        return owner_resources._release_event_owners(self, owners)

    async def wait_owner_events(self, owner: object, timeout: float) -> bool:
        return await owner_resources.wait_owner_events(self, owner, timeout)

    async def _unload_owner_resources(
        self, owner: object, mode: StopMode
    ) -> ShutdownReport:
        """Settle in-flight work and release resources owned by one scope."""

        return await owner_resources._unload_owner_resources(self, owner, mode)

    def _has_owner_resources(self, owner: object) -> bool:
        return owner_resources._has_owner_resources(self, owner)

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
        return await shutdown.stop(self, mode)

    close = stop

    async def teardown(self) -> ShutdownReport:
        return await self.stop(self._stop_requested)

    async def _close(self) -> ShutdownReport:
        return await shutdown._close(self)

    async def _close_impl(self) -> ShutdownReport:
        return await shutdown._close_impl(self)

    async def _rollback_startup(self) -> None:
        return await startup._rollback_startup(self)

    async def _call_startup(
        self,
        component: Any,
        method_name: str,
        timeout: float,
        *args: Any,
    ) -> None:
        return await components._call_startup(self, component, method_name, timeout, *args)

    async def _cleanup(
        self,
        component: Any,
        phase: str,
        argument: Any,
        report: ShutdownReport,
        deadline: float,
    ) -> bool:
        return await components._cleanup(self, component, phase, argument, report, deadline)

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
            if self.failure_cause is None:
                self.failure_cause = error
            self._stop_requested = "abort"
            if self._close_task is None:
                self._close_task = self.tasks.spawn(
                    self._close(),
                    name="runtime:critical-abort",
                    owner="runtime-close",
                    critical=False,
                )

    async def __aenter__(self) -> "Runtime":
        return await self.lifespan().__aenter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return await self.lifespan().__aexit__(exc_type, exc, tb)
