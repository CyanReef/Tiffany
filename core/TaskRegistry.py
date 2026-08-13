from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
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


class TaskRegistry:
    """Owns every long-lived task created by the runtime and its components."""

    __slots__ = (
        "_tasks",
        "_info",
        "_next_id",
        "_closing",
        "_failure_handler",
        "_failures",
    )

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._info: dict[asyncio.Task[Any], TaskInfo] = {}
        self._next_id = 1
        self._closing = False
        self._failure_handler: Callable[[TaskInfo, BaseException], Any] | None = None
        self._failures: list[tuple[TaskInfo, BaseException]] = []

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
        task = asyncio.create_task(awaitable, name=name)
        info = TaskInfo(task_id, name, owner, critical, failure_policy)
        self._tasks.add(task)
        self._info[task] = info
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        info = self._info.pop(task, None)
        self._tasks.discard(task)
        if info is None:
            return
        if task.cancelled():
            if not self._closing and (
                info.critical or info.failure_policy == "disable_owner"
            ):
                self._report_failure(
                    info,
                    RuntimeError(f"task {info.name!r} cancelled unexpectedly"),
                )
            return
        error = task.exception()
        if error is not None:
            self._failures.append((info, error))
            if info.critical or info.failure_policy == "disable_owner":
                logger.error(
                    "supervised task %r owned by %r failed",
                    info.name,
                    info.owner,
                    exc_info=error,
                )
                self._report_failure(info, error)
            else:
                logger.debug(
                    "supervised non-critical task %r owned by %r failed",
                    info.name,
                    info.owner,
                    exc_info=error,
                )

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
    def failures(self) -> tuple[tuple[TaskInfo, BaseException], ...]:
        return tuple(self._failures)

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
