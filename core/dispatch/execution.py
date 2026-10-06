"""Hook invocation, timeout/error policies, metrics and payload-free tracing."""
from __future__ import annotations

import asyncio
import inspect
import logging
from time import perf_counter

from ..Context import Context
from ..Metrics import MetricRegistry
from ..Trace import DispatchPhase, DispatchRecord, DispatchResult, TraceRecorder
from .models import HookExecutionError, HookRegistration, HookTimeoutError

# Preserve the public entry point's logger for operators and existing filters.
logger = logging.getLogger("core.Dispatcher")
_HOOK_METRICS = ("hook_executions_total", "hook_duration_seconds_count", "hook_duration_seconds_sum")


class HookExecutor:
    __slots__ = ("metrics", "trace")

    def __init__(self, metrics: MetricRegistry | None, trace: TraceRecorder | None) -> None:
        self.metrics = metrics if metrics is not None else MetricRegistry()
        self.trace = trace if trace is not None else TraceRecorder()

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
            logger.error("hook %r failed event_id=%s phase=%s reason=%s: %s",
                         hook.name, ctx.envelope.event_id, phase,
                         type(error).__name__, error)
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
                return
            raise HookTimeoutError(hook.name, hook.timeout or 0.0) from error
        except Exception as error:
            logger.error("hook %r failed event_id=%s phase=%s reason=%s: %s",
                         hook.name, ctx.envelope.event_id, phase,
                         type(error).__name__, error)
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
        bound = self.metrics._bind(_HOOK_METRICS, labels)
        self.metrics._batch((("inc", bound, (1.0, 1.0, duration)),))
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
