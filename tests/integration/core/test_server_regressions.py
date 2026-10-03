import asyncio
import unittest
from unittest.mock import patch

from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from core import Bot, Envelope, Field, RuntimeState, ServiceKey, ServiceOwnershipError, ShutdownIncompleteError


class ServerCoreRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_intentional_scope_task_cancellation_keeps_runtime_running(self):
        bot = Bot()
        scope = bot.scope("tasks")
        await bot.start()
        task = bot.runtime.tasks.spawn(asyncio.Event().wait(), name="owned", owner=scope.owner)
        await scope.unload(mode="abort")
        self.assertTrue(task.cancelled())
        self.assertIsNone(bot.runtime.failure_cause)
        self.assertEqual(bot.runtime.state, RuntimeState.RUNNING)
        await bot.stop()

    async def test_drain_uses_one_budget_for_events_and_owned_tasks(self):
        bot = Bot()
        scope = bot.scope("budget")
        entered = asyncio.Event()
        @scope.hook()
        async def work(ctx):
            entered.set()
            await asyncio.sleep(.06)
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        await entered.wait()
        task = bot.runtime.tasks.spawn(asyncio.sleep(.12), name="owned", owner=scope.owner, critical=False)
        with patch.object(type(bot.runtime), "DRAIN_TIMEOUT", .08):
            await scope.unload()
        await future
        self.assertTrue(task.cancelled())  # separate full budgets would let it finish
        await bot.stop()

    async def wait_running(self, bot):
        async with asyncio.timeout(2):
            while bot.runtime.state != RuntimeState.RUNNING:
                await asyncio.sleep(0)

    async def test_run_async_finishes_after_normal_stop(self):
        bot = Bot()
        runner = asyncio.create_task(bot.run_async())
        await self.wait_running(bot)
        await bot.stop()
        await asyncio.wait_for(runner, 1)
        self.assertTrue((await bot.runtime.wait_closed()).successful)

    async def test_critical_failure_wakes_runner_and_preserves_first_cause(self):
        bot = Bot()
        runner = asyncio.create_task(bot.run_async())
        await self.wait_running(bot)
        cause = RuntimeError("critical failure")
        async def failed():
            raise cause
        with self.assertLogs("core.TaskRegistry", level="ERROR"):
            bot.runtime.tasks.spawn(failed(), name="broken", owner="test")
            with self.assertRaises(RuntimeError) as result:
                await asyncio.wait_for(runner, 2)
        self.assertIs(result.exception, cause)
        self.assertIs(bot.runtime.failure_cause, cause)
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)

    async def test_startup_rollback_notifies_waiters(self):
        class Failing:
            async def start(self):
                raise ValueError("startup")
        bot = Bot()
        bot.service(ServiceKey("failing"), Failing())
        with self.assertRaises(ValueError):
            await bot.start()
        report = await asyncio.wait_for(bot.runtime.wait_closed(), 1)
        self.assertIs(report, bot.runtime.shutdown_report)
        self.assertTrue(bot.runtime.startup_failed)

    async def test_abort_unload_cancels_immediately_and_blocks_registration(self):
        bot = Bot()
        scope = bot.scope("test")
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        @scope.hook()
        async def blocked(ctx):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        await entered.wait()
        unload = asyncio.create_task(scope.unload(mode="abort"))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "unloading"):
            scope.provide(Field("late"), lambda ctx: 1)
        await asyncio.wait_for(unload, 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(future.cancelled())
        await bot.stop()

    async def test_stubborn_cancel_keeps_services_until_retry(self):
        bot = Bot()
        scope = bot.scope("stubborn")
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Resource:
            closed = False
            async def teardown(self):
                self.closed = True
        resource = Resource()
        key = ServiceKey("resource")
        scope.service(key, resource)
        @scope.hook(uses=(key,))
        async def stubborn(ctx):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                self.assertIs(ctx.service(key), resource)
        await bot.start()
        future = await bot.runtime.emit(Envelope("test", {}), wait=False)
        await entered.wait()
        try:
            with patch.object(type(bot.runtime), "CANCEL_GRACE", 0.01):
                with self.assertRaises(ShutdownIncompleteError):
                    await scope.unload(mode="abort")
            self.assertTrue(cancelled.is_set())
            self.assertFalse(resource.closed)
            self.assertIs(bot.services.get(key), resource)
        finally:
            release.set()
            await future
            await scope.unload()
            await bot.stop()
        self.assertTrue(resource.closed)

    async def test_service_instance_rejects_multiple_owners(self):
        bot = Bot()
        shared = object()
        key = ServiceKey("shared")
        bot.scope("a").service(key, shared)
        with self.assertRaises(ServiceOwnershipError):
            bot.scope("b").service(ServiceKey("alias"), shared)
        self.assertIs(bot.services.get(key), shared)

    async def test_background_hook_error_is_logged_once_and_does_not_stop_runtime(self):
        bot = Bot()
        @bot.hook(name="broken")
        async def broken(ctx):
            raise ValueError("hook reason")
        await bot.start()
        with self.assertLogs("core.Dispatcher", level="ERROR") as logs:
            future = await bot.runtime.emit(Envelope("test", {}, event_id="correlation"), wait=False)
            with self.assertRaises(Exception):
                await future
        self.assertEqual(len(logs.output), 1)
        self.assertIn("broken", logs.output[0])
        self.assertIn("correlation", logs.output[0])
        self.assertIn("hook reason", logs.output[0])
        self.assertEqual(bot.runtime.state, RuntimeState.RUNNING)
        await bot.stop()

    async def test_metrics_count_each_adapter_and_global_queue(self):
        bot = Bot()
        release, entered = asyncio.Event(), asyncio.Event()
        @bot.hook()
        async def blocked(ctx):
            entered.set()
            await release.wait()
        await bot.start()
        bot.runtime.scheduler.global_limit = 1
        active = await bot.runtime.emit(Envelope("test", {}, adapter_id="a", session_id="active"), wait=False)
        await entered.wait()
        a = await bot.runtime.emit(Envelope("test", {}, adapter_id="a", session_id="queued"), wait=False)
        b = await bot.runtime.emit(Envelope("test", {}, adapter_id="b", session_id="queued"), wait=False)
        sample = bot.metrics.snapshot()
        self.assertEqual(sample.get("event_queue_depth"), 2)
        self.assertEqual(sample.get("event_queue_depth", {"adapter": "a"}), 1)
        self.assertEqual(sample.get("event_queue_depth", {"adapter": "b"}), 1)
        self.assertEqual(sample.get("events_active", {"adapter": "a"}), 1)
        self.assertEqual(sample.get("events_active", {"adapter": "b"}), 0)
        release.set()
        await asyncio.gather(active, a, b)
        await bot.stop()
        self.assertEqual(bot.metrics.snapshot().get("events_active"), 0)

    async def test_onebot_unload_revokes_owned_provider_handles(self):
        bot = Bot()
        scope = bot.scope("adapter")
        scope.install(OneBotWebSocketAdapter("127.0.0.1", 0, "test"))
        await bot.setup()
        self.assertEqual(len(bot.providers.registrations()), 6)
        await scope.unload(mode="abort")
        self.assertEqual(bot.providers.registrations(), ())
        await bot.stop()

    async def test_partial_onebot_provider_setup_rolls_back_new_handles(self):
        from adapters.onebot_fields import register_onebot_fields
        from fields import MESSAGE_TYPE
        bot = Bot()
        existing = bot.provide(MESSAGE_TYPE, lambda ctx: "custom", platform="test")
        with self.assertRaises(ValueError):
            register_onebot_fields(bot, "test")
        self.assertEqual(bot.providers.registrations(), (existing,))
        await bot.stop()

    async def test_critical_failure_during_start_waits_for_startup_transaction(self):
        bot = Bot()
        failed = asyncio.Event()
        class Adapter:
            platform = "test"
            adapter_id = "starting"
            async def start(self):
                async def fail():
                    failed.set()
                    raise RuntimeError("startup background failure")
                bot.runtime.tasks.spawn(fail(), name="early-failure", owner=self)
                await failed.wait()
                await asyncio.sleep(0.01)
            async def teardown(self):
                pass
        bot.install(Adapter())
        with self.assertLogs("core.TaskRegistry", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "startup background failure"):
                await asyncio.wait_for(bot.run_async(), 2)
        self.assertTrue((await bot.runtime.wait_closed()).successful)
