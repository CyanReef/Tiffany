"""Shared shutdown transaction, drain escalation and completion notification."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..Lifecycle import RuntimeState, ShutdownReport, StopMode

if TYPE_CHECKING:
    from ..Runtime import Runtime


async def stop(runtime: Runtime, mode: StopMode = "drain") -> ShutdownReport:
    if mode not in ("drain", "abort"):
        raise ValueError(f"unknown stop mode: {mode!r}")
    if mode == "abort":
        runtime._stop_requested = "abort"
        if runtime.state == RuntimeState.STOPPING_DRAIN:
            runtime.scheduler.request_abort()
    if runtime.state in (RuntimeState.TERMINATED, RuntimeState.STOP_FAILED):
        return runtime.shutdown_report or ShutdownReport()
    task = runtime._close_task
    if task is None:
        task = runtime.tasks.spawn(
            runtime._close(),
            name="runtime:close",
            owner="runtime-close",
            critical=False,
        )
        runtime._close_task = task
    return await asyncio.shield(task)


async def _close(runtime: Runtime) -> ShutdownReport:
    try:
        # A critical task may fail during adapter.start(). Let the startup
        # transaction finish or roll back before releasing its resources.
        async with runtime._start_lock:
            return await runtime._close_impl()
    except BaseException as error:
        report = runtime.shutdown_report or ShutdownReport(forced=True)
        report.failures.append(error)
        runtime.shutdown_report = report
        runtime.state = RuntimeState.STOP_FAILED
        return report
    finally:
        runtime._closed.set()


async def _close_impl(runtime: Runtime) -> ShutdownReport:
    if runtime.state in (RuntimeState.TERMINATED, RuntimeState.STOP_FAILED):
        return runtime.shutdown_report or ShutdownReport()
    report = ShutdownReport(startup_results=tuple(runtime.startup_results))
    shutdown_started = time.perf_counter()
    runtime.shutdown_report = report
    runtime.scheduler.stop_admission()
    drain_started = time.perf_counter()
    running = runtime.state == RuntimeState.RUNNING
    if runtime._stop_requested == "drain" and running:
        runtime.state = RuntimeState.STOPPING_DRAIN
        try:
            async with asyncio.timeout(runtime.DRAIN_TIMEOUT):
                events_drained = await runtime.scheduler.drain(runtime.DRAIN_TIMEOUT)
                adapters_drained = True
                if events_drained:
                    for component in reversed(runtime._started_adapters):
                        if runtime._stop_requested == "abort":
                            adapters_drained = False
                            break
                        component_deadline = (
                            asyncio.get_running_loop().time()
                            + runtime.COMPONENT_TIMEOUT
                        )
                        adapters_drained = (
                            await runtime._cleanup(
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
        runtime.metrics.observe("runtime_drain_seconds", report.drain_duration)
        if not drained:
            runtime._stop_requested = "abort"
    elif not running:
        runtime._stop_requested = "abort"
    if runtime._stop_requested == "abort":
        report.forced = True
        runtime.metrics.inc("runtime_forced_shutdown_total")
        runtime.state = RuntimeState.STOPPING_ABORT

    loop = asyncio.get_running_loop()
    deadline = loop.time() + runtime.ABORT_TIMEOUT
    if report.forced:
        abandoned = await runtime.scheduler.abort(runtime.CANCEL_GRACE)
        report.abandoned_tasks = abandoned

    for component in reversed(runtime._started_adapters):
        if report.forced:
            await runtime._cleanup(component, "stop", "abort", report, deadline)
    runtime._started_adapters.clear()
    runtime._started_adapter_ids.clear()

    runtime.state = RuntimeState.STOPPED
    runtime.state = RuntimeState.TEARING_DOWN
    for registration in reversed(runtime._entered_lifespans):
        await runtime._cleanup(
            registration.manager,
            "__aexit__",
            None,
            report,
            deadline,
        )
    runtime._entered_lifespans.clear()
    for component in reversed(runtime._started_services):
        await runtime._cleanup(component, "stop", runtime._stop_requested, report, deadline)
    runtime._started_services.clear()
    runtime._started_service_ids.clear()
    for component in reversed(runtime._setup_components):
        await runtime._cleanup(component, "teardown", None, report, deadline)
    runtime._setup_components.clear()
    runtime._setup_ids.clear()

    abandoned = await runtime.tasks.close(runtime.CANCEL_GRACE)
    if abandoned:
        report.abandoned_tasks = tuple(dict.fromkeys(
            (*report.abandoned_tasks, *abandoned)
        ))
    if runtime.scheduler.queued or runtime.scheduler.active or runtime._owner_events:
        residue = RuntimeError(
            "runtime shutdown left event queues or owner references"
        )
        report.failures.append(residue)
    runtime.state = (
        RuntimeState.TERMINATED if report.successful else RuntimeState.STOP_FAILED
    )
    runtime.metrics.observe(
        "runtime_shutdown_seconds",
        time.perf_counter() - shutdown_started,
        labels={"reason": "forced" if report.forced else "drained"},
    )
    return report
