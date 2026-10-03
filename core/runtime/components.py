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
        runtime.startup_results.append(StartupResult(
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
        runtime.startup_results.append(StartupResult(
            component=_component_name(component),
            phase=method_name,
            outcome="error",
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
