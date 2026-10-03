"""Startup transaction and reverse rollback; Runtime owns the shared state."""
from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

from ..Lifecycle import LifecycleError, RuntimeState, ShutdownReport

if TYPE_CHECKING:
    from ..Runtime import Runtime


async def setup(runtime: Runtime) -> None:
    async with runtime._start_lock:
        if runtime.state == RuntimeState.SETUP:
            return
        if runtime.state != RuntimeState.NEW or runtime._failed_start:
            raise LifecycleError(f"runtime cannot setup from {runtime.state.value!r}")
        runtime.state = RuntimeState.SETTING_UP
        try:
            services = [handle.component for handle in runtime.bot.services.lifecycle_order()]
            runtime._service_order = services
            components: list[Any] = []
            component_ids: set[int] = set()
            for component in (*services, *runtime._adapters):
                if id(component) in component_ids:
                    continue
                component_ids.add(id(component))
                components.append(component)
            async with asyncio.timeout(runtime.STARTUP_TIMEOUT):
                for component in components:
                    await runtime._call_startup(
                        component,
                        "setup",
                        runtime.SETUP_TIMEOUT,
                        runtime,
                    )
                    runtime._setup_components.append(component)
                    runtime._setup_ids.add(id(component))
            # Adapter setup may install protocol-owned synchronous
            # providers. Freeze and validate only after every component
            # has declared its capabilities, before any I/O starts.
            runtime.bot.services.validate()
            runtime.bot._validate_registrations()
            runtime.state = RuntimeState.SETUP
        except BaseException:
            runtime._failed_start = True
            await runtime._rollback_startup()
            raise


async def start(runtime: Runtime) -> None:
    if runtime.state == RuntimeState.NEW:
        await runtime.setup()
    async with runtime._start_lock:
        if runtime.state == RuntimeState.RUNNING:
            return
        if runtime.state != RuntimeState.SETUP or runtime._failed_start:
            raise LifecycleError(f"runtime cannot start from {runtime.state.value!r}")
        runtime.state = RuntimeState.STARTING
        try:
            runtime.bot.services.validate()
            runtime.bot._validate_registrations()
            async with asyncio.timeout(runtime.STARTUP_TIMEOUT):
                for component in runtime._service_order:
                    if id(component) in runtime._started_service_ids:
                        continue
                    await runtime._call_startup(
                        component,
                        "start",
                        runtime.START_TIMEOUT,
                    )
                    runtime._started_services.append(component)
                    runtime._started_service_ids.add(id(component))
                for registration in runtime._lifespans:
                    await runtime._call_startup(
                        registration.manager,
                        "__aenter__",
                        runtime.LIFESPAN_TIMEOUT,
                    )
                    runtime._entered_lifespans.append(registration)
                for adapter in runtime._adapters:
                    if id(adapter) in runtime._started_adapter_ids:
                        continue
                    await runtime._call_startup(
                        adapter,
                        "start",
                        runtime.START_TIMEOUT,
                    )
                    runtime._started_adapters.append(adapter)
                    runtime._started_adapter_ids.add(id(adapter))
            runtime.scheduler.start()
            runtime.state = RuntimeState.RUNNING
        except BaseException:
            runtime._failed_start = True
            await runtime._rollback_startup()
            raise


async def _rollback_startup(runtime: Runtime) -> None:
    report = ShutdownReport(forced=True)
    report.startup_results = tuple(runtime.startup_results)
    runtime.shutdown_report = report
    deadline = asyncio.get_running_loop().time() + runtime.ABORT_TIMEOUT
    runtime.scheduler.stop_admission()
    report.abandoned_tasks = await runtime.scheduler.abort(runtime.CANCEL_GRACE)
    for component in reversed(runtime._started_adapters):
        await runtime._cleanup(component, "stop", "abort", report, deadline)
    for registration in reversed(runtime._entered_lifespans):
        await runtime._cleanup(
            registration.manager,
            "__aexit__",
            None,
            report,
            deadline,
        )
    for component in reversed(runtime._started_services):
        await runtime._cleanup(component, "stop", "abort", report, deadline)
    for component in reversed(runtime._setup_components):
        await runtime._cleanup(component, "teardown", None, report, deadline)
    runtime._started_adapters.clear()
    runtime._started_adapter_ids.clear()
    runtime._entered_lifespans.clear()
    runtime._started_services.clear()
    runtime._started_service_ids.clear()
    runtime._setup_components.clear()
    runtime._setup_ids.clear()
    abandoned = await runtime.tasks.close(runtime.CANCEL_GRACE)
    if abandoned:
        report.abandoned_tasks = abandoned
    runtime.state = RuntimeState.STOP_FAILED
    runtime._closed.set()
