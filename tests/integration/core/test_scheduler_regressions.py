"""Cancellation, task isolation and failure boundaries of direct fair dispatch."""
import asyncio
from contextvars import ContextVar
import unittest
from unittest.mock import patch

from core import Bot, Envelope, MetricRegistry, RuntimeState, ServiceKey
from core.EventScheduler import EventScheduler
from core.TaskRegistry import TaskRegistry


class DirectSchedulerRegressions(unittest.IsolatedAsyncioTestCase):
    def assert_settled(self, bot):
        scheduler = bot.runtime.scheduler
        self.assertEqual((scheduler.queued, scheduler.active), (0, 0))
        self.assertEqual(bot.runtime._owner_events, {})
        self.assertEqual(scheduler._lanes, {})
        self.assertEqual(scheduler._ready, {})
        self.assertEqual(scheduler._runnable_set, set())
        self.assertEqual(scheduler.buffered_bytes, 0)
        self.assertEqual(scheduler._adapter_queued, {})
        self.assertEqual(scheduler._adapter_active, {})
        self.assertEqual(bot.metrics.snapshot().get("events_active"), 0)
        self.assertEqual(bot.metrics.snapshot().get("event_queue_depth"), 0)

    async def test_completed_adapters_do_not_accumulate_queue_counters(self):
        bot = Bot()
        async with bot:
            for index in range(100):
                await bot.emit(Envelope("test", {}, adapter_id=f"source-{index}"))
                self.assert_settled(bot)

    async def test_task_cancelled_before_first_step_releases_reservation(self):
        bot = Bot()
        calls = []
        @bot.hook()
        async def work(ctx):
            calls.append(ctx)
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}, event_id="never-started"), wait=False)
        task, = bot.runtime.tasks.tasks_for("events")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await future
        self.assertEqual(calls, [])
        self.assert_settled(bot)
        self.assertTrue((await bot.stop()).successful)

    async def test_cancelled_background_future_does_not_cancel_accepted_work(self):
        bot = Bot()
        completed = asyncio.Event()
        @bot.hook()
        async def work(ctx):
            completed.set()
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        future.cancel()
        await asyncio.wait_for(completed.wait(), 1)
        self.assertTrue((await bot.stop()).successful)
        self.assert_settled(bot)

    async def test_cancelled_queued_waiter_leaves_no_lane_or_owner(self):
        bot = Bot()
        bot.runtime.scheduler.global_limit = 0
        await bot.start()
        waiter = asyncio.create_task(bot.emit(Envelope("test", {}, session_id="queued")))
        await asyncio.sleep(0)
        self.assertEqual(bot.runtime.scheduler.queued, 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assert_settled(bot)
        self.assertTrue((await bot.stop()).successful)

    async def test_queued_scope_unload_cancels_before_dispatch(self):
        bot = Bot()
        scope = bot.scope("queued")
        bot.runtime.scheduler.global_limit = 0
        calls = []
        @scope.hook()
        async def work(ctx):
            calls.append(ctx)
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        await scope.unload(mode="abort")
        self.assertTrue(future.cancelled())
        self.assertEqual(calls, [])
        self.assert_settled(bot)
        await bot.stop()

    async def test_cancelled_active_waiter_cancels_event_and_releases_owner(self):
        bot = Bot()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        @bot.hook()
        async def work(ctx):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await bot.start()
        waiter = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await cancelled.wait()
        self.assert_settled(bot)
        await bot.stop()

    async def test_stubborn_event_keeps_active_slot_and_service_until_completion(self):
        bot = Bot()
        scope = bot.scope("stubborn-waiter")
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Resource:
            closed = False
            async def teardown(self):
                self.closed = True
        resource = Resource()
        key = ServiceKey("resource")
        scope.service(key, resource)
        @scope.hook(uses=(key,))
        async def work(ctx):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            self.assertIs(ctx.service(key), resource)
            self.assertFalse(resource.closed)
        await bot.start()
        waiter = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await cancelled.wait()
        self.assertEqual(bot.runtime.scheduler.active, 1)
        self.assertEqual(bot.runtime.scheduler.buffered_bytes, 4096)
        self.assertEqual(bot.metrics.snapshot().get("events_active"), 1)
        self.assertTrue(bot.runtime._owner_events)
        release.set()
        await scope.unload()
        self.assertTrue(resource.closed)
        self.assert_settled(bot)
        await bot.stop()

    async def test_completion_and_waiter_cancellation_race_is_idempotent(self):
        bot = Bot()
        entered, release = asyncio.Event(), asyncio.Event()
        @bot.hook()
        async def work(ctx):
            entered.set()
            await release.wait()
        await bot.start()
        for index in range(20):
            entered.clear()
            release.clear()
            waiter = asyncio.create_task(bot.emit(Envelope("test", {}, session_id="same")))
            await entered.wait()
            release.set()
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            await asyncio.sleep(0)
            self.assert_settled(bot)
        await bot.stop()

    async def test_each_event_has_isolated_startup_context_and_task(self):
        marker = ContextVar("scheduler-regression", default="default")
        token = marker.set("startup")
        bot, seen, tasks = Bot(), [], []
        @bot.hook()
        async def work(ctx):
            seen.append(marker.get())
            tasks.append(asyncio.current_task())
            marker.set("previous-event")
            await asyncio.sleep(0)
        try:
            await bot.start()
            marker.set("submitter")
            await bot.emit(Envelope("test", {}, event_id="first"))
            await bot.emit(Envelope("test", {}, event_id="second"))
            self.assertEqual(seen, ["startup", "startup"])
            self.assertEqual(marker.get(), "submitter")
            self.assertIsNot(tasks[0], tasks[1])
            self.assertEqual([task.get_name() for task in tasks], ["event:first", "event:second"])
        finally:
            await bot.stop()
            marker.reset(token)

    async def test_dynamic_limits_dispatch_waiting_work_and_do_not_cancel_running_work(self):
        bot = Bot()
        scheduler = bot.runtime.scheduler
        scheduler.global_limit = 1
        scheduler.configure_adapter("a", 1)
        gates = [asyncio.Event() for _ in range(3)]
        @bot.hook()
        async def work(ctx):
            await gates[ctx.raw["index"]].wait()
        await bot.start()
        futures = [await bot.runtime.emit(Envelope("test", {"index": i}, adapter_id="a", session_id=str(i)), wait=False) for i in range(3)]
        self.assertEqual((scheduler.active, scheduler.queued), (1, 2))
        scheduler.global_limit = 2
        self.assertEqual(scheduler.active, 1)
        scheduler.configure_adapter("a", 2)
        self.assertEqual((scheduler.active, scheduler.queued), (2, 1))
        scheduler.global_limit = 1
        scheduler.configure_adapter("a", 1)
        gates[0].set()
        await futures[0]
        self.assertEqual((scheduler.active, scheduler.queued), (1, 1))
        gates[1].set()
        await futures[1]
        self.assertEqual((scheduler.active, scheduler.queued), (1, 0))
        gates[2].set()
        await futures[2]
        self.assert_settled(bot)
        await bot.stop()

    async def test_registry_replacement_does_not_reuse_stale_bindings(self):
        bot = Bot()
        entered, release = asyncio.Event(), asyncio.Event()
        @bot.hook(name="tracked")
        async def work(ctx):
            entered.set()
            await release.wait()
        await bot.start()
        old = bot.metrics
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        await entered.wait()
        new = MetricRegistry()
        bot.runtime.metrics = bot.dispatcher.metrics = new
        release.set()
        await future
        await bot.emit(Envelope("test", {}))
        self.assertEqual(old.snapshot().get("hook_executions_total", {"platform": "test", "hook": "tracked", "reason": "completed"}), 0)
        self.assertEqual(new.snapshot().get("hook_executions_total", {"platform": "test", "hook": "tracked", "reason": "completed"}), 2)
        self.assert_settled(bot)
        await bot.stop()

    async def test_task_creation_failure_rolls_back_and_closes_runtime(self):
        bot = Bot()
        cause = RuntimeError("cannot create event task")
        original = TaskRegistry.spawn
        def spawn(registry, coroutine, **kwargs):
            if kwargs["name"].startswith("event:"):
                raise cause
            return original(registry, coroutine, **kwargs)
        await bot.start()
        with patch.object(TaskRegistry, "spawn", spawn), self.assertLogs("core.EventScheduler", level="ERROR"):
            future = await bot.runtime.emit(Envelope("test", {}), wait=False)
            with self.assertRaises(RuntimeError) as error:
                await future
            self.assertIs(error.exception, cause)
            self.assertTrue((await asyncio.wait_for(bot.runtime.wait_closed(), 1)).successful)
        self.assertIs(bot.runtime.failure_cause, cause)
        self.assert_settled(bot)
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)

    async def test_admission_internal_failure_closes_and_cancels_queued_future(self):
        bot = Bot()
        bot.runtime.scheduler.global_limit = 0
        cause = RuntimeError("ready queue failed")
        await bot.start()
        with patch.object(EventScheduler, "_mark_ready", side_effect=cause), self.assertLogs("core.EventScheduler", level="ERROR"):
            future = await bot.runtime.emit(Envelope("test", {}), wait=False)
            with self.assertRaises(asyncio.CancelledError):
                await future
            self.assertTrue((await asyncio.wait_for(bot.runtime.wait_closed(), 1)).successful)
        self.assertIs(bot.runtime.failure_cause, cause)
        self.assert_settled(bot)

    async def test_pump_internal_failure_releases_reserved_work(self):
        bot = Bot()
        cause = RuntimeError("queue metric transition failed")
        original, failed = EventScheduler._record_state, False
        def record(scheduler, adapter_id, **kwargs):
            nonlocal failed
            if not kwargs and not failed:
                failed = True
                raise cause
            return original(scheduler, adapter_id, **kwargs)
        await bot.start()
        with patch.object(EventScheduler, "_record_state", record), self.assertLogs("core.EventScheduler", level="ERROR"):
            future = await bot.runtime.emit(Envelope("test", {}), wait=False)
            with self.assertRaises(RuntimeError):
                await future
            self.assertTrue((await asyncio.wait_for(bot.runtime.wait_closed(), 1)).successful)
        self.assertIs(bot.runtime.failure_cause, cause)
        self.assert_settled(bot)

    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "Python 3.12+")
    async def test_immediate_tasks_and_reentrant_admission_remain_fair(self):
        bot, seen = Bot(), []
        @bot.hook()
        async def work(ctx):
            marker = ctx.raw["marker"]
            seen.append(marker)
            if marker == 1:
                await bot.runtime.emit(Envelope("test", {"marker": 2}, session_id="second"), wait=False)
        await bot.start()
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        try:
            loop.set_task_factory(asyncio.eager_task_factory)
            await bot.emit(Envelope("test", {"marker": 1}, session_id="first"))
            await asyncio.sleep(0)
            self.assertEqual(seen, [1, 2])
            self.assert_settled(bot)
            self.assertEqual(bot.runtime.scheduler._event_work, {})
        finally:
            loop.set_task_factory(previous)
            await bot.stop()

    async def test_completion_internal_failure_closes_remaining_session_queue(self):
        bot = Bot()
        entered, release = asyncio.Event(), asyncio.Event()
        cause = RuntimeError("completion state recording failed")
        @bot.hook()
        async def work(ctx):
            entered.set()
            await release.wait()
        await bot.start()
        try:
            first = await bot.runtime.emit(Envelope("test", {}, session_id="same"), wait=False)
            await entered.wait()
            second = await bot.runtime.emit(Envelope("test", {}, session_id="same"), wait=False)
            original, failed = EventScheduler._record_state, False
            def record_state(scheduler, adapter_id, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise cause
                return original(scheduler, adapter_id, **kwargs)
            with patch.object(EventScheduler, "_record_state", record_state), self.assertLogs("core.EventScheduler", level="ERROR"):
                release.set()
                await first
                await asyncio.wait_for(bot.runtime.wait_closed(), 1)
            self.assertIs(bot.runtime.failure_cause, cause)
            self.assertTrue(second.cancelled())
            self.assert_settled(bot)
        finally:
            await bot.stop("abort")


if __name__ == "__main__":
    unittest.main()
