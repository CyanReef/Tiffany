from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import Context
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .Ownership import same_owner


logger = logging.getLogger(__name__)
T = TypeVar("T")
FailurePolicy = Literal["abort_runtime", "disable_owner"]


@dataclass(frozen=True, slots=True)
class TaskInfo:
    task_id: int
    name: str
    owner: object
    critical: bool
    failure_policy: FailurePolicy


@dataclass(frozen=True, slots=True)
class TaskFailureRecord:
    """Bounded diagnostic metadata; never retains a task's object graph."""

    task_id: int
    name: str
    owner_label: str
    critical: bool
    failure_policy: FailurePolicy
    error_type: str


class TaskRegistry:
    """Owns every long-lived task created by the runtime and its components."""

    __slots__ = (
        "_tasks",
        "_info",
        "_next_id",
        "_closing",
        "_failure_handler",
        "_failures",
        "_expected_cancellations",
        "_failure_count",
        "_failures_dropped",
    )

    def __init__(self, *, failure_capacity: int = 256) -> None:
        if type(failure_capacity) is not int or failure_capacity < 1:
            raise ValueError("failure_capacity must be a positive integer")
        self._tasks: set[asyncio.Task[Any]] = set()
        self._info: dict[asyncio.Task[Any], TaskInfo] = {}
        self._next_id = 1
        self._closing = False
        self._failure_handler: Callable[[TaskInfo, BaseException], Any] | None = None
        self._failures: deque[TaskFailureRecord] = deque(maxlen=failure_capacity)
        self._failure_count = self._failures_dropped = 0
        self._expected_cancellations: set[asyncio.Task[Any]] = set()

    def set_failure_handler(
        self,
        handler: Callable[[TaskInfo, BaseException], Any],
    ) -> None:
        self._failure_handler = handler

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        name: str,
        owner: object,
        critical: bool = True,
        failure_policy: FailurePolicy = "abort_runtime",
        context: Context | None = None,
    ) -> asyncio.Task[T]:
        if failure_policy not in ("abort_runtime", "disable_owner"):
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise ValueError(f"unknown task failure policy: {failure_policy!r}")
        if self._closing:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError("task registry is closing")

        task_id = self._next_id
        self._next_id += 1
        try:
            task = asyncio.create_task(awaitable, name=name, context=context)
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        info = TaskInfo(task_id, name, owner, critical, failure_policy)
        self._tasks.add(task)
        self._info[task] = info
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        info = self._info.pop(task, None)
        self._tasks.discard(task)
        expected_cancel = task in self._expected_cancellations
        self._expected_cancellations.discard(task)
        if info is None:
            return
        if task.cancelled():
            if not self._closing and not expected_cancel and (
                info.critical or info.failure_policy == "disable_owner"
            ):
                error = RuntimeError(f"task {info.name!r} cancelled unexpectedly")
                self._remember_failure(info, error)
                self._report_failure(info, error)
            return
        error = task.exception()
        if error is not None:
            self._remember_failure(info, error)
            if info.critical or info.failure_policy == "disable_owner":
                logger.error(
                    "supervised task %r owned by %r failed",
                    info.name,
                    _owner_label(info.owner),
                    exc_info=error,
                )
                self._report_failure(info, error)
            else:
                logger.debug(
                    "supervised non-critical task %r owned by %r failed",
                    info.name,
                    _owner_label(info.owner),
                    exc_info=error,
                )

    def _remember_failure(self, info: TaskInfo, error: BaseException) -> None:
        self._failure_count += 1
        if len(self._failures) == self._failures.maxlen:
            self._failures_dropped += 1
        self._failures.append(TaskFailureRecord(
            info.task_id, info.name[:256], _owner_label(info.owner),
            info.critical, info.failure_policy,
            f"{type(error).__module__}.{type(error).__qualname__}"[:256],
        ))

    def _report_failure(self, info: TaskInfo, error: BaseException) -> None:
        if self._failure_handler is None:
            return
        try:
            result = self._failure_handler(info, error)
            if inspect.isawaitable(result):
                # Failure handling must itself be supervised.
                self.spawn(
                    result,
                    name=f"task-failure:{info.name}",
                    owner="runtime",
                    critical=False,
                )
        except Exception:
            logger.exception("task failure handler failed")

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def snapshot(self) -> tuple[TaskInfo, ...]:
        return tuple(self._info.values())

    @property
    def failures(self) -> tuple[TaskFailureRecord, ...]:
        return tuple(self._failures)

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def failures_dropped(self) -> int:
        return self._failures_dropped

    def stacks(self) -> dict[int, tuple[Any, ...]]:
        return {
            info.task_id: tuple(task.get_stack())
            for task, info in self._info.items()
        }

    def tasks_for(self, owner: object) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            task
            for task, info in self._info.items()
            if same_owner(info.owner, owner)
        )

    async def wait_owner(self, owner: object, timeout: float | None = None) -> bool:
        tasks = self.tasks_for(owner)
        if not tasks:
            return True
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks),
                    return_exceptions=True,
                )
            return True
        except TimeoutError:
            return False

    async def cancel_owner(self, owner: object, grace: float = 1.0) -> tuple[str, ...]:
        tasks = self.tasks_for(owner)
        for task in tasks:
            self._expected_cancellations.add(task)
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=grace)
            for task in done:
                if not task.cancelled():
                    task.exception()
        else:
            pending = set()
        return tuple(task.get_name() for task in pending)
    async def close(self, grace: float = 1.0) -> tuple[str, ...]:
        self._closing = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        if not tasks:
            return ()
        done, pending = await asyncio.wait(tasks, timeout=grace)
        for task in done:
            if not task.cancelled():
                task.exception()
        return tuple(task.get_name() for task in pending)


def _owner_label(owner: object) -> str:
    if isinstance(owner, str):
        return owner[:256]
    return f"{type(owner).__module__}.{type(owner).__qualname__}"[:256]
