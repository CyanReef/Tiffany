from __future__ import annotations

import asyncio
from collections import deque
from contextvars import Context as TaskContext, copy_context
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Literal

from .Lifecycle import RuntimeOverloadedError
from .Ownership import same_owner
from .SchedulerPolicy import SchedulerPolicy

if TYPE_CHECKING:
    from .Context import Context
    from .Envelope import Envelope
    from .Runtime import Runtime

logger = logging.getLogger(__name__)
_QUEUE_METRICS = ("event_queue_depth", "events_active", "event_queue_capacity")
_WATERMARK = ("event_queue_watermark",)
_RECEIVED = ("events_received_total",)


class _Lane(deque):
    __slots__ = ("bytes",)


class _DrainWaiter:
    __slots__ = ("event", "users")

    def __init__(self):
        self.event = asyncio.Event()
        self.users = 0


@dataclass(slots=True)
class _Work:
    envelope: "Envelope"
    future: "asyncio.Future[Context]"
    adapter_id: str
    lane: tuple[str, object]
    snapshot: Any = None
    task: asyncio.Task[Any] | None = None
    owners: tuple[object, ...] = ()
    state: Literal["queued", "active", "finished"] = "queued"
    charge: int = 4096


class EventScheduler:
    """Bounded session lanes, synchronously pumped into isolated event tasks."""

    __slots__ = (
        "runtime", "capacity", "_global_limit", "buffer_budget_bytes",
        "_lanes", "_ready", "_runnable", "_runnable_set", "_active_sessions",
        "_adapter_active", "_adapter_queued", "_adapter_limits", "_adapter_idle",
        "_queued", "_active", "_bytes", "_idle", "_accepting", "_started",
        "_aborting", "_pumping", "_pump_again", "_event_context",
        "_event_work",
    )

    def __init__(self, runtime: "Runtime", *, policy: SchedulerPolicy | None = None) -> None:
        policy = SchedulerPolicy() if policy is None else policy
        if not isinstance(policy, SchedulerPolicy):
            raise TypeError("scheduler must be a SchedulerPolicy")
        self.runtime = runtime
        self.capacity = policy.max_events
        self.buffer_budget_bytes = policy.buffer_budget_bytes
        self._global_limit = policy.max_concurrency
        self._lanes: dict[tuple[str, object], _Lane] = {}
        self._ready: dict[str, dict[tuple[str, object], None]] = {}
        self._runnable: deque[str] = deque()
        self._runnable_set: set[str] = set()
        self._active_sessions: set[tuple[str, object]] = set()
        self._adapter_active: dict[str, int] = {}
        self._adapter_queued: dict[str, int] = {}
        self._adapter_limits: dict[str, int | None] = {}
        self._adapter_idle: dict[str, _DrainWaiter] = {}
        self._queued = self._active = self._bytes = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._accepting = self._started = self._aborting = False
        self._pumping = self._pump_again = False
        self._event_context: TaskContext | None = None
        self._event_work: dict[asyncio.Task[Any], _Work] = {}

    @property
    def global_limit(self) -> int:
        return self._global_limit

    @global_limit.setter
    def global_limit(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("global limit must be a non-negative integer")
        self._global_limit = value
        for adapter_id in self._ready:
            self._enable_adapter(adapter_id)
        self._pump()

    def configure_adapter(self, adapter_id: str, active_limit: int | None = None) -> None:
        if active_limit is not None and (type(active_limit) is not int or active_limit < 1):
            raise ValueError("adapter active limit must be a positive integer or None")
        self._adapter_limits[adapter_id] = active_limit
        self._enable_adapter(adapter_id)
        self._pump()

    def _adapter_limit(self, adapter_id: str) -> int:
        limit = self._adapter_limits.get(adapter_id)
        return self._global_limit if limit is None else limit

    def start(self) -> None:
        self._event_context = copy_context()
        self._started = self._accepting = True
        self._pump()

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
        return (self._accepting and self._queued + self._active < self.capacity
                and self._bytes < self.buffer_budget_bytes)

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    def snapshot(self) -> dict[str, int]:
        """Read on the runtime loop; no per-event metric writes or lane scan."""
        return {
            "event_buffer_estimated_bytes": self._bytes,
            "event_buffer_budget_bytes": self.buffer_budget_bytes,
            "event_buffer_available_bytes": max(0, self.buffer_budget_bytes - self._bytes),
            "event_slots_available": max(0, self.capacity - self._active - self._queued),
            "event_reserved_slots": self.capacity // 8,
            "event_reserved_bytes": self.buffer_budget_bytes // 8,
            "event_concurrency_limit": self._global_limit,
        }

    def submit(self, envelope: "Envelope", *, reject: bool, background: bool) -> _Work | None:
        adapter_id = envelope.adapter_id
        charge = envelope.admission_bytes
        if charge is None:
            charge = 4096
        elif type(charge) is not int or charge < 1:
            raise ValueError("admission_bytes must be a positive integer or None")
        total = self._queued + self._active + 1
        used_bytes = self._bytes + charge
        if not self._accepting or total > self.capacity:
            return self._reject(envelope, adapter_id, "capacity", reject)
        if used_bytes > self.buffer_budget_bytes:
            return self._reject(envelope, adapter_id, "buffer_budget", reject)
        session_id = envelope.session_id
        if session_id is None:
            lane = (adapter_id, object())
        else:
            lane = (adapter_id, session_id)
        queue = self._lanes.get(lane)
        if (total > self.capacity - self.capacity // 8
                or used_bytes > self.buffer_budget_bytes - self.buffer_budget_bytes // 8):
            lane_count = (len(queue) if queue is not None else 0) + (lane in self._active_sessions) + 1
            lane_bytes = (queue.bytes if queue is not None else 0) + charge
            if (session_id is None or lane_count > 32
                    or lane_bytes > min(256 * 1024, self.buffer_budget_bytes // 8)):
                return self._reject(envelope, adapter_id, "reserved_capacity", reject)
        # Reject before allocating a Future, registration snapshot or owner hold.
        snapshot, owners = self.runtime._capture_event(envelope)
        if queue is None:
            queue = self._lanes[lane] = _Lane()
            queue.bytes = 0
        future = asyncio.get_running_loop().create_future()
        work = _Work(envelope, future, adapter_id, lane, snapshot, owners=owners, charge=charge)
        self.runtime._retain_event_owners(owners)
        queue.append(work)
        queue.bytes += charge
        self._bytes = used_bytes
        self._queued += 1
        self._adapter_queued[adapter_id] = self._adapter_queued.get(adapter_id, 0) + 1
        self._idle.clear()
        if background:
            future.add_done_callback(_consume_future)
        try:
            self._record_state(adapter_id, received_platform=envelope.platform)
            self._pump(work)
        except BaseException as error:
            self._fatal(error)
        return work

    def cancel_waiter(self, work: _Work) -> None:
        if work.state == "queued":
            self._discard_queued(work)
            self._pump()
        elif work.state == "active" and work.task is not None:
            work.task.cancel()
        work.future.cancel()

    def _reject(self, envelope: "Envelope", adapter_id: str, reason: str, reject: bool) -> None:
        self.runtime.metrics.inc("events_rejected_total", labels={
            "platform": envelope.platform, "adapter": adapter_id, "reason": reason,
        })
        if reject:
            raise RuntimeOverloadedError(f"runtime event admission rejected: {reason}")
        return None

    def _enable_adapter(self, adapter_id: str) -> None:
        if (self._ready.get(adapter_id) and adapter_id not in self._runnable_set
                and self._adapter_active.get(adapter_id, 0) < self._adapter_limit(adapter_id)):
            self._runnable.append(adapter_id)
            self._runnable_set.add(adapter_id)

    def _mark_ready(self, lane: tuple[str, object]) -> None:
        if lane not in self._active_sessions and self._lanes.get(lane):
            adapter_id = lane[0]
            ready = self._ready.get(adapter_id)
            if ready is None:
                ready = self._ready[adapter_id] = {}
            ready[lane] = None
            self._enable_adapter(adapter_id)

    def _pump(self, submitted: _Work | None = None) -> None:
        # With no runnable predecessor, avoid allocating ready bookkeeping
        # for a lane that can start immediately. The activation and Task path
        # below is shared with queued work, including eager/reentrant Tasks.
        immediate = None
        if submitted is not None:
            if (not self._pumping and not self._runnable
                    and self._started and not self._aborting
                    and self._active < self._global_limit
                    and submitted.lane not in self._active_sessions
                    and not self._ready.get(submitted.adapter_id)
                    and self._adapter_active.get(submitted.adapter_id, 0) < self._adapter_limit(submitted.adapter_id)):
                immediate = submitted
            else:
                self._mark_ready(submitted.lane)
        if not self._started or self._aborting:
            return
        if self._pumping:
            self._pump_again = True
            return
        self._pumping = True
        work = None
        try:
            while True:
                self._pump_again = False
                while self._active < self._global_limit and (immediate is not None or self._runnable) and not self._aborting:
                    if immediate is not None:
                        adapter_id, lane = immediate.adapter_id, immediate.lane
                        immediate = None
                        active = self._adapter_active.get(adapter_id, 0)
                    else:
                        adapter_id = self._runnable.popleft()
                        self._runnable_set.discard(adapter_id)
                        active = self._adapter_active.get(adapter_id, 0)
                        if active >= self._adapter_limit(adapter_id):
                            continue
                        ready = self._ready[adapter_id]
                        lane = next(iter(ready))
                        del ready[lane]
                        if not ready:
                            del self._ready[adapter_id]
                    work = self._lanes[lane].popleft()
                    work.state = "active"
                    self._queued -= 1
                    self._adapter_queued[adapter_id] -= 1
                    if not self._adapter_queued[adapter_id]:
                        self._adapter_queued.pop(adapter_id)
                    self._active += 1
                    self._adapter_active[adapter_id] = active + 1
                    self._active_sessions.add(lane)
                    if self._ready.get(adapter_id):
                        self._enable_adapter(adapter_id)
                    self._record_state(adapter_id)
                    self._launch(work)
                if not self._pump_again or self._aborting:
                    break
        except BaseException as error:
            self._fatal(error)
            if work is not None and work.state == "active" and work.task is None:
                if not work.future.done():
                    work.future.set_exception(error)
                self._finish_work(work)
        finally:
            self._pumping = False

    async def wait_adapter_idle(self, adapter_id: str) -> None:
        if not (self._adapter_active.get(adapter_id, 0) or self._adapter_queued.get(adapter_id, 0)):
            return
        waiter = self._adapter_idle.get(adapter_id)
        if waiter is None:
            waiter = self._adapter_idle[adapter_id] = _DrainWaiter()
        waiter.users += 1
        try:
            while self._adapter_active.get(adapter_id, 0) or self._adapter_queued.get(adapter_id, 0):
                waiter.event.clear()
                await waiter.event.wait()
        finally:
            waiter.users -= 1
            if not waiter.users:
                self._adapter_idle.pop(adapter_id, None)

    def _launch(self, work: _Work) -> None:
        context = self._event_context.copy()
        coroutine = self._execute(work)
        try:
            task = context.run(
                self.runtime.tasks.spawn,
                coroutine,
                name=f"event:{work.envelope.event_id}",
                owner="events",
                critical=False,
            )
        except BaseException:
            coroutine.close()
            raise
        work.task = task
        self._event_work[task] = work
        task.add_done_callback(self._event_done, context=self._event_context.copy())

    async def _execute(self, work: _Work) -> None:
        try:
            ctx = await self.runtime._dispatch(work.envelope, self._queued, work.snapshot)
            if not work.future.done():
                work.future.set_result(ctx)
        except asyncio.CancelledError:
            work.future.cancel()
            raise
        except BaseException as error:
            if not work.future.done():
                work.future.set_exception(error)
        finally:
            self._finish_work(work)

    def _finish_work(self, work: _Work) -> None:
        if work.state == "finished":
            return
        try:
            previous, work.state = work.state, "finished"
            self.runtime._release_event_owners(work.owners)
            if previous == "active":
                self._active -= 1
                self._adapter_active[work.adapter_id] -= 1
                if not self._adapter_active[work.adapter_id]:
                    self._adapter_active.pop(work.adapter_id)
                self._active_sessions.discard(work.lane)
            else:
                self._queued -= 1
                self._adapter_queued[work.adapter_id] -= 1
                if not self._adapter_queued[work.adapter_id]:
                    self._adapter_queued.pop(work.adapter_id)
            queue = self._lanes[work.lane]
            queue.bytes -= work.charge
            self._bytes -= work.charge
            if not queue and (previous == "active" or work.lane not in self._active_sessions):
                self._lanes.pop(work.lane, None)
                ready = self._ready.get(work.adapter_id)
                if ready is not None:
                    ready.pop(work.lane, None)
                    if not ready:
                        self._ready.pop(work.adapter_id)
                        if work.adapter_id in self._runnable_set:
                            self._runnable_set.discard(work.adapter_id)
                            self._runnable.remove(work.adapter_id)
                    else:
                        self._enable_adapter(work.adapter_id)
            elif queue and previous != "active":
                self._mark_ready(work.lane)
            if self.idle:
                self._idle.set()
            if self._adapter_idle:
                waiter = self._adapter_idle.get(work.adapter_id)
                if waiter is not None:
                    waiter.event.set()
            self._record_state(work.adapter_id)
            if previous == "active":
                # Reuse direct activation when this lane has no runnable
                # predecessor. Otherwise _pump appends it after ready peers.
                self._pump(queue[0] if queue else None)
        except BaseException as error:
            self._fatal(error)

    def _discard_queued(self, work: _Work) -> None:
        if work.state != "queued":
            return
        self._lanes[work.lane].remove(work)
        work.future.cancel()
        self._finish_work(work)

    def _event_done(self, task: asyncio.Task[Any]) -> None:
        work = self._event_work.pop(task, None)
        if work is not None and work.state != "finished":
            # Pre-start cancellation never enters _execute's finally block.
            if not work.future.done():
                if task.cancelled():
                    work.future.cancel()
                else:
                    error = task.exception()
                    if error is not None:
                        work.future.set_exception(error)
            self._finish_work(work)

    def _fatal(self, error: BaseException) -> None:
        if not self._aborting:
            logger.error("event scheduler failed: %s", error, exc_info=error)
        self._accepting = False
        self._aborting = True
        self._idle.set()
        self.runtime._request_critical_abort(error)

    def stop_admission(self) -> None:
        self._accepting = False
        self._pump()

    def request_abort(self) -> None:
        self._aborting = True
        self._idle.set()

    async def drain(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                while not self.idle:
                    if self._aborting or self.runtime._stop_requested == "abort":
                        return False
                    await self._idle.wait()
                return True
        except TimeoutError:
            return False

    async def abort(self, grace: float = 1.0) -> tuple[str, ...]:
        self._accepting = False
        self.request_abort()
        queued = tuple(work for queue in self._lanes.values() for work in queue)
        for work in queued:
            self._discard_queued(work)
        tasks = tuple(
            task for task, work in self._event_work.items()
            if work.state == "active"
        )
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
        else:
            pending = set()
        return tuple(task.get_name() for task in pending)

    def _record_state(
        self, adapter_id: str, *, received_platform: str | None = None,
    ) -> None:
        metrics = self.runtime.metrics
        queued = self._adapter_queued.get(adapter_id, 0)
        active = self._adapter_active.get(adapter_id, 0)
        labels = {"adapter": adapter_id}
        updates = [
            ("set", metrics._bind(_QUEUE_METRICS), (self._queued, self._active, self.capacity)),
            ("set", metrics._bind(_QUEUE_METRICS, labels), (queued, active, self.capacity)),
        ]
        if received_platform is not None:
            updates.extend((
                ("max", metrics._bind(_WATERMARK), (self._queued + self._active,)),
                ("max", metrics._bind(_WATERMARK, labels), (queued + active,)),
                ("inc", metrics._bind(_RECEIVED, {"platform": received_platform, "adapter": adapter_id}), (1.0,)),
            ))
        metrics._batch(tuple(updates))

    async def cancel_owner(self, owner: object, grace: float = 1.0) -> tuple[str, ...]:
        queued = tuple(
            work for queue in self._lanes.values() for work in queue
            if any(same_owner(item, owner) for item in work.owners)
        )
        for work in queued:
            self._discard_queued(work)
        tasks = tuple(
            task for task, work in self._event_work.items()
            if work.state == "active"
            and any(same_owner(item, owner) for item in work.owners)
        )
        for task in tasks:
            task.cancel()
        self._pump()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
        else:
            pending = set()
        return tuple(task.get_name() for task in pending)


def _consume_future(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
