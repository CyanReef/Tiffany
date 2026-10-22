"""Bounded lifecycle calls and structured results for individual components."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Literal, TYPE_CHECKING

from ..Lifecycle import CleanupResult, ComponentStartupTimeoutError, ShutdownReport, StartupResult

if TYPE_CHECKING:
    from ..Runtime import Runtime


async def _call_startup(
    runtime: Runtime,
    component: Any,
    method_name: str,
    timeout: float,
    *args: Any,
) -> None:
    if runtime._startup_deadline is not None and asyncio.get_running_loop().time() >= runtime._startup_deadline:
        error = ComponentStartupTimeoutError(_component_name(component), method_name, 0.0)
        runtime.startup_results.append(StartupResult(_component_name(component), method_name,
                                                     "timeout", 0.0, type(error).__name__, str(error)))
        raise error
    method = getattr(component, method_name, None)
    if method is None:
        _record_acquired(runtime, component, method_name)
        return
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    deadline = min(loop.time() + timeout, runtime._startup_deadline or float("inf"))
    allowed = max(0.0, deadline - loop.time())
    finished_at = []

    async def invoke_startup() -> None:
        try:
            result = method(*args)
            if inspect.isawaitable(result):
                await result
            # Journal inside the task: a late successful acquisition still
            # belongs to rollback, even after its caller has timed out.
            _record_acquired(runtime, component, method_name)
        finally:
            finished_at.append(loop.time())

    task = runtime.tasks.spawn(
        invoke_startup(), name=f"startup:{_component_name(component)}:{method_name}",
        owner="runtime-startup", critical=False, context=runtime._lifecycle_context,
    )
    runtime._startup_pending[task] = component
    task.add_done_callback(lambda done: runtime._startup_pending.pop(done, None))
    timed_out = False
    try:
        done, _ = await asyncio.wait((task,), timeout=allowed)
        timed_out = task not in done or bool(finished_at and finished_at[0] > deadline)
        if timed_out:
            raise ComponentStartupTimeoutError(_component_name(component), method_name, allowed)
        await task
    except BaseException as error:
        if not task.done():
            task.cancel()
            # asyncio.wait does not wait indefinitely for cancellation-resistant
            # code, unlike awaiting the task or wait_for().
            await asyncio.wait((task,), timeout=runtime.CANCEL_GRACE)
        runtime.startup_results.append(StartupResult(
            component=_component_name(component),
            phase=method_name,
            outcome="timeout" if timed_out else "error",
            duration=time.perf_counter() - started,
            error_type=type(error).__name__,
            error_message=_redact_error(error),
        ))
        raise
    else:
        runtime.startup_results.append(StartupResult(
            component=_component_name(component),
            phase=method_name,
            outcome="completed",
            duration=time.perf_counter() - started,
        ))


def _record_acquired(runtime: Runtime, component: Any, phase: str) -> None:
    if phase == "setup":
        runtime._setup_components.append(component)
        runtime._setup_ids.add(id(component))
    elif phase == "start":
        if any(item is component for item in runtime._adapters):
            runtime._started_adapters.append(component)
            runtime._started_adapter_ids.add(id(component))
        else:
            runtime._started_services.append(component)
            runtime._started_service_ids.add(id(component))
    elif phase == "__aenter__":
        registration = next(item for item in runtime._lifespans if item.manager is component)
        runtime._entered_lifespans.append(registration)


async def _cleanup(
    runtime: Runtime,
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
    timeout = min(runtime.COMPONENT_TIMEOUT, remaining)
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

        task = runtime.tasks.spawn(
            invoke_cleanup(),
            name=f"cleanup:{_component_name(component)}:{phase}",
            owner="runtime-cleanup",
            critical=False,
            context=runtime._lifecycle_context,
        )
        done, _ = await asyncio.wait((task,), timeout=timeout)
        if task not in done:
            task.cancel()
            _, pending = await asyncio.wait(
                (task,),
                timeout=min(runtime.CANCEL_GRACE, max(
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


def _component_name(component: Any) -> str:
    return getattr(component, "name", None) or type(component).__name__


def _redact_error(error: BaseException) -> str:
    text = str(error).replace("\n", " ")
    return text[:256]


def _remove_identity(items: list[Any], value: object) -> bool:
    for index, item in enumerate(items):
        if item is value:
            items.pop(index)
            return True
    return False
