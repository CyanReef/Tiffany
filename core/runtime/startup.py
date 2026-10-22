"""Startup transaction and reverse rollback; Runtime owns the shared state."""
from __future__ import annotations

import asyncio
from contextvars import copy_context
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
        if runtime._lifecycle_context is None:
            runtime._lifecycle_context = copy_context()
        runtime._startup_deadline = asyncio.get_running_loop().time() + runtime.STARTUP_TIMEOUT
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
            for component in components:
                await runtime._call_startup(
                    component, "setup", runtime.SETUP_TIMEOUT, runtime,
                )
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
        finally:
            runtime._startup_deadline = None


async def start(runtime: Runtime) -> None:
    if runtime.state == RuntimeState.NEW:
        await runtime.setup()
    async with runtime._start_lock:
        if runtime.state == RuntimeState.RUNNING:
            return
        if runtime.state != RuntimeState.SETUP or runtime._failed_start:
            raise LifecycleError(f"runtime cannot start from {runtime.state.value!r}")
        runtime.state = RuntimeState.STARTING
        runtime._startup_deadline = asyncio.get_running_loop().time() + runtime.STARTUP_TIMEOUT
        try:
            runtime.bot.services.validate()
            runtime.bot._validate_registrations()
            for component in runtime._service_order:
                if id(component) not in runtime._started_service_ids:
                    await runtime._call_startup(component, "start", runtime.START_TIMEOUT)
            for registration in runtime._lifespans:
                await runtime._call_startup(registration.manager, "__aenter__", runtime.LIFESPAN_TIMEOUT)
            for adapter in runtime._adapters:
                if id(adapter) not in runtime._started_adapter_ids:
                    await runtime._call_startup(adapter, "start", runtime.START_TIMEOUT)
            runtime._lifecycle_context.run(runtime.scheduler.start)
            runtime.state = RuntimeState.RUNNING
        except BaseException:
            runtime._failed_start = True
            await runtime._rollback_startup()
            raise
        finally:
            runtime._startup_deadline = None


async def _rollback_startup(runtime: Runtime) -> None:
    report = ShutdownReport(forced=True)
    report.startup_results = tuple(runtime.startup_results)
    runtime.shutdown_report = report
    deadline = asyncio.get_running_loop().time() + runtime.ABORT_TIMEOUT
    runtime.scheduler.stop_admission()
    report.abandoned_tasks = await runtime.scheduler.abort(runtime.CANCEL_GRACE)
    pending = {task: component for task, component in runtime._startup_pending.items() if not task.done()}
    if pending:
        # Keep the whole acquisition transaction alive while startup code may
        # still access its dependencies. A later stop can retry after settlement.
        runtime._startup_retry = True
        report.abandoned_tasks = tuple(dict.fromkeys((*report.abandoned_tasks,
                                                     *(task.get_name() for task in pending))))
        from .components import _component_name
        report.abandoned_components = tuple(dict.fromkeys(
            _component_name(component) for component in
            (*runtime._setup_components,
             *(registration.manager for registration in runtime._entered_lifespans),
             *pending.values())
        ))
        runtime.state = RuntimeState.STOP_FAILED
        runtime._closed.set()
        return
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
    runtime._startup_retry = False
    runtime.state = RuntimeState.STOP_FAILED
    runtime._closed.set()
