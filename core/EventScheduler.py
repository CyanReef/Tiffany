from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .Lifecycle import RuntimeOverloadedError
from .Ownership import same_owner


if TYPE_CHECKING:
    from .Context import Context
    from .Envelope import Envelope
    from .Runtime import Runtime


@dataclass(slots=True)
class _Work:
    envelope: "Envelope"
    future: asyncio.Future["Context"]
    adapter_id: str
    session_id: str | None
    snapshot: Any = None
    task: asyncio.Task[Any] | None = None
    owners: tuple[object, ...] = ()


class EventScheduler:
    """Bounded FIFO session-lane scheduler with fair runnable-lane selection."""

    __slots__ = (
        "runtime",
        "capacity",
        "global_limit",
        "session_backlog",
        "_lanes",
        "_ready",
        "_ready_set",
        "_active_sessions",
        "_adapter_active",
        "_adapter_queued",
        "_adapter_limits",
        "_queued",
        "_active",
        "_wake",
        "_accepting",
        "_scheduler_task",
        "_event_tasks",
        "_event_work",
        "_sequence",
        "_changed",
    )

    def __init__(
        self,
        runtime: "Runtime",
        *,
        capacity: int = 256,
        global_limit: int = 16,
        session_backlog: int = 32,
    ) -> None:
        self.runtime = runtime
        self.capacity = capacity
        self.global_limit = global_limit
        self.session_backlog = session_backlog
        self._lanes: dict[tuple[str, str], deque[_Work]] = {}
        self._ready: deque[tuple[str, str]] = deque()
        self._ready_set: set[tuple[str, str]] = set()
        self._active_sessions: set[tuple[str, str]] = set()
        self._adapter_active: dict[str, int] = {}
        self._adapter_queued: dict[str, int] = {}
        self._adapter_limits: dict[str, int] = {}
        self._queued = 0
        self._active = 0
        self._wake = asyncio.Event()
        self._accepting = False
        self._scheduler_task: asyncio.Task[None] | None = None
        self._event_tasks: set[asyncio.Task[Any]] = set()
        self._event_work: dict[asyncio.Task[Any], _Work] = {}
        self._sequence = 0
        self._changed = asyncio.Condition()

    def configure_adapter(self, adapter_id: str, active_limit: int = 4) -> None:
        if active_limit < 1:
            raise ValueError("adapter active limit must be at least 1")
        self._adapter_limits[adapter_id] = active_limit

    def start(self) -> None:
        self._accepting = True
        self._scheduler_task = self.runtime.tasks.spawn(
            self._run(),
            name="runtime:event-scheduler",
            owner="runtime",
            critical=True,
        )

    @property
    def queued(self) -> int:
        return self._queued

    @property
    def active(self) -> int:
        return self._active

    @property
    def idle(self) -> bool:
        return self._queued == 0 and self._active == 0

    @property
    def accepting(self) -> bool:
        return self._accepting and self._queued + self._active < self.capacity

    async def submit(
        self,
        envelope: "Envelope",
        *,
        adapter_id: str,
        reject: bool,
        snapshot: Any = None,
        wait: bool = True,
        owners: tuple[object, ...] = (),
    ) -> "Context | asyncio.Future[Context] | None":
        if not self._accepting or self._queued + self._active >= self.capacity:
            self.runtime.metrics.inc(
                "events_rejected_total",
                labels={"platform": envelope.platform, "adapter": adapter_id,
                        "reason": "capacity"},
            )
            if reject:
                raise RuntimeOverloadedError("runtime event capacity is full")
            return None

        session_id = envelope.session_id
        if session_id is None:
            self._sequence += 1
            lane = (adapter_id, f"__unscoped__:{self._sequence}")
        else:
            lane = (adapter_id, session_id)
        queue = self._lanes.setdefault(lane, deque())
        if session_id is not None and len(queue) >= self.session_backlog:
            self.runtime.metrics.inc(
                "events_rejected_total",
                labels={"platform": envelope.platform, "adapter": adapter_id,
                        "reason": "session_backlog"},
            )
            if reject:
                raise RuntimeOverloadedError("session event backlog is full")
            return None

        future = asyncio.get_running_loop().create_future()
        work = _Work(envelope, future, adapter_id, session_id, snapshot, None, owners)
        self.runtime._retain_event_owners(owners)
        queue.append(work)
        self._queued += 1
        self._adapter_queued[adapter_id] = self._adapter_queued.get(adapter_id, 0) + 1
        self.runtime.metrics.inc(
            "events_received_total",
            labels={"platform": envelope.platform, "adapter": adapter_id},
        )
        self.runtime.metrics.set("event_queue_depth", self._queued,
                                 labels={"adapter": adapter_id})
        self.runtime.metrics.set("event_queue_capacity", self.capacity,
                                 labels={"adapter": adapter_id})
        self._record_watermark(adapter_id)
        self._mark_ready(lane)
        self._wake.set()
        if not wait:
            future.add_done_callback(_consume_future)
            return future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if work.task is not None:
                work.task.cancel()
            elif work in queue:
                queue.remove(work)
                self._queued -= 1
                self._adapter_queued[adapter_id] -= 1
                if not queue:
                    self._lanes.pop(lane, None)
                    self._ready_set.discard(lane)
                    try:
                        self._ready.remove(lane)
                    except ValueError:
                        pass
                self.runtime.metrics.set(
                    "event_queue_depth",
                    self._queued,
                    labels={"adapter": adapter_id},
                )
                self._record_watermark(adapter_id)
                self.runtime._release_event_owners(work.owners)
            future.cancel()
            self._wake.set()
            raise

    def _mark_ready(self, lane: tuple[str, str]) -> None:
        if lane in self._active_sessions or lane in self._ready_set:
            return
        if self._lanes.get(lane):
            self._ready.append(lane)
            self._ready_set.add(lane)

    async def _run(self) -> None:
        while self._accepting or self._queued or self._active:
            made_progress = False
            scans = len(self._ready)
            while self._active < self.global_limit and scans:
                scans -= 1
                lane = self._ready.popleft()
                self._ready_set.discard(lane)
                adapter_id = lane[0]
                adapter_active = self._adapter_active.get(adapter_id, 0)
                limit = self._adapter_limits.get(adapter_id, 4)
                if adapter_active >= limit:
                    self._mark_ready(lane)
                    continue
                queue = self._lanes.get(lane)
                if not queue:
                    self._lanes.pop(lane, None)
                    continue
                work = queue.popleft()
                self._queued -= 1
                self._adapter_queued[adapter_id] -= 1
                self._active += 1
                self._adapter_active[adapter_id] = adapter_active + 1
                self._active_sessions.add(lane)
                self.runtime.metrics.set("event_queue_depth", self._queued,
                                         labels={"adapter": adapter_id})
                self.runtime.metrics.set("events_active", self._active,
                                         labels={"adapter": adapter_id})
                self._record_watermark(adapter_id)
                task = self.runtime.tasks.spawn(
                    self._execute(lane, work),
                    name=f"event:{work.envelope.event_id}",
                    owner="events",
                    critical=False,
                )
                work.task = task
                self._event_tasks.add(task)
                self._event_work[task] = work
                task.add_done_callback(self._event_done)
                made_progress = True
            if not made_progress:
                self._wake.clear()
                await self._wake.wait()

    async def _execute(self, lane: tuple[str, str], work: _Work) -> None:
        try:
            ctx = await self.runtime._dispatch(
                work.envelope,
                self._queued,
                work.snapshot,
            )
            if not work.future.done():
                work.future.set_result(ctx)
        except asyncio.CancelledError:
            if not work.future.done():
                work.future.cancel()
            raise
        except BaseException as error:
            if not work.future.done():
                work.future.set_exception(error)
        finally:
            self.runtime._release_event_owners(work.owners)
            self._active -= 1
            adapter_id = work.adapter_id
            count = self._adapter_active.get(adapter_id, 1) - 1
            if count:
                self._adapter_active[adapter_id] = count
            else:
                self._adapter_active.pop(adapter_id, None)
            self._active_sessions.discard(lane)
            if self._lanes.get(lane):
                self._mark_ready(lane)
            else:
                self._lanes.pop(lane, None)
            self.runtime.metrics.set("events_active", self._active,
                                     labels={"adapter": adapter_id})
            self._record_watermark(adapter_id)
            async with self._changed:
                self._changed.notify_all()
            self._wake.set()

    def stop_admission(self) -> None:
        self._accepting = False
        self._wake.set()

    def request_abort(self) -> None:
        self._wake.set()

    async def drain(self, timeout: float) -> bool:
        if self.idle:
            return True
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if self.runtime._stop_requested == "abort":
                        return False
                    if self.idle:
                        return True
                    self._wake.clear()
                    if self.runtime._stop_requested == "abort":
                        return False
                    if self.idle:
                        return True
                    await self._wake.wait()
        except TimeoutError:
            return False

    async def abort(self, grace: float = 1.0) -> tuple[str, ...]:
        self._accepting = False
        for queue in self._lanes.values():
            while queue:
                work = queue.popleft()
                if not work.future.done():
                    work.future.cancel()
                self.runtime._release_event_owners(work.owners)
        self._lanes.clear()
        self._ready.clear()
        self._ready_set.clear()
        self._queued = 0
        for adapter_id in self._adapter_queued:
            self._adapter_queued[adapter_id] = 0
            self._record_watermark(adapter_id)
        tasks = tuple(self._event_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
        else:
            pending = set()
        self._wake.set()
        return tuple(task.get_name() for task in pending)

    def _record_watermark(self, adapter_id: str) -> None:
        metrics = self.runtime.metrics
        queued = self._adapter_queued.get(adapter_id, 0)
        active = self._adapter_active.get(adapter_id, 0)
        for labels, depth, running in ((None, self._queued, self._active),
                                        ({"adapter": adapter_id}, queued, active)):
            metrics.set("event_queue_depth", depth, labels=labels)
            metrics.set("events_active", running, labels=labels)
            metrics.set_max("event_queue_watermark", depth + running, labels=labels)
        metrics.set("event_queue_capacity", self.capacity)

    async def cancel_owner(
        self,
        owner: object,
        grace: float = 1.0,
    ) -> tuple[str, ...]:
        """Cancel queued and active events whose captured route owns owner."""

        for lane, queue in tuple(self._lanes.items()):
            retained: deque[_Work] = deque()
            while queue:
                work = queue.popleft()
                if any(same_owner(item, owner) for item in work.owners):
                    self._queued -= 1
                    self._adapter_queued[work.adapter_id] -= 1
                    self._record_watermark(work.adapter_id)
                    if not work.future.done():
                        work.future.cancel()
                    self.runtime._release_event_owners(work.owners)
                else:
                    retained.append(work)
            if retained:
                self._lanes[lane] = retained
            else:
                self._lanes.pop(lane, None)
                self._ready_set.discard(lane)
                try:
                    self._ready.remove(lane)
                except ValueError:
                    pass

        tasks = tuple(
            task
            for task, work in self._event_work.items()
            if any(same_owner(item, owner) for item in work.owners)
        )
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
        else:
            pending = set()
        self._wake.set()
        async with self._changed:
            self._changed.notify_all()
        return tuple(task.get_name() for task in pending)

    def _event_done(self, task: asyncio.Task[Any]) -> None:
        self._event_tasks.discard(task)
        self._event_work.pop(task, None)


def _consume_future(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
