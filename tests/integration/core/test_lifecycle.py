"""Runtime startup, rollback and shutdown contracts."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from core import (
    Bot,
    DuplicateAdapterIdError,
    Envelope,
    RuntimeNotRunningError,
    RuntimeState,
    ServiceKey,
)


class Component:
    def __init__(self, name, calls, *, fail=None, gate=None):
        self.name = name
        self.calls = calls
        self.fail = fail
        self.gate = gate

    async def setup(self, runtime):
        self.calls.append(f"setup:{self.name}")
        if self.fail == "setup":
            raise RuntimeError(self.name)

    async def start(self):
        self.calls.append(f"start:{self.name}")
        if self.fail == "start":
            raise RuntimeError(self.name)

    async def stop(self, mode):
        self.calls.append(f"stop:{self.name}:{mode}")
        if self.gate is not None:
            await self.gate.wait()

    async def teardown(self):
        self.calls.append(f"teardown:{self.name}")


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_ignores_unrelated_host_tasks(self):
        bot = Bot()
        host_task = asyncio.create_task(
            asyncio.Event().wait(), name="unrelated-host-task"
        )
        try:
            await bot.start()
            report = await bot.stop()
            self.assertTrue(report.successful)
            self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)
            self.assertFalse(host_task.done())
            self.assertNotIn("unrelated-host-task", report.abandoned_tasks)
        finally:
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)

    async def test_emit_requires_running_and_close_is_shared(self):
        bot = Bot()
        with self.assertRaises(RuntimeNotRunningError):
            await bot.emit(Envelope("test", {}))

        await bot.setup()
        await bot.start()
        await bot.emit(Envelope("test", {}))
        first, second = await asyncio.gather(bot.stop(), bot.stop())
        self.assertIs(first, second)
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)
        self.assertEqual(bot.runtime.tasks.active_count, 0)

    async def test_service_dependency_order_and_reverse_cleanup(self):
        calls = []
        first_key = ServiceKey[Component]("first")
        second_key = ServiceKey[Component]("second")
        bot = Bot()
        bot.service(first_key, Component("first", calls))
        bot.service(
            second_key,
            Component("second", calls),
            dependencies=(first_key,),
        )

        await bot.setup()
        await bot.start()
        await bot.stop("abort")

        self.assertEqual(calls, [
            "setup:first", "setup:second",
            "start:first", "start:second",
            "stop:second:abort", "stop:first:abort",
            "teardown:second", "teardown:first",
        ])

    async def test_start_failure_rolls_back_and_cannot_retry(self):
        calls = []
        bot = Bot()
        bot.service(ServiceKey("good"), Component("good", calls))
        bot.service(ServiceKey("bad"), Component("bad", calls, fail="start"))

        await bot.setup()
        with self.assertRaisesRegex(RuntimeError, "bad"):
            await bot.start()
        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)
        self.assertIn("stop:good:abort", calls)
        self.assertLess(calls.index("teardown:bad"), calls.index("teardown:good"))
        with self.assertRaises(RuntimeError):
            await bot.start()

    async def test_cancelling_waiter_does_not_cancel_cleanup(self):
        calls = []
        gate = asyncio.Event()
        bot = Bot()
        bot.service(ServiceKey("slow"), Component("slow", calls, gate=gate))
        await bot.setup()
        await bot.start()

        waiter = asyncio.create_task(bot.stop("abort"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        await bot.stop("abort")
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)

    async def test_start_rollback_only_stops_components_that_started(self):
        calls = []
        bot = Bot()
        first = Component("first", calls)
        failing = Component("failing", calls, fail="start")
        never_started = Component("never", calls)
        bot.install(first)
        bot.install(failing)
        bot.install(never_started)

        await bot.setup()
        with self.assertRaisesRegex(RuntimeError, "failing"):
            await bot.start()

        self.assertIn("stop:first:abort", calls)
        self.assertNotIn("stop:failing:abort", calls)
        self.assertNotIn("stop:never:abort", calls)
        self.assertLess(calls.index("teardown:never"), calls.index("teardown:first"))

    async def test_duplicate_adapter_instance_id_is_rejected(self):
        calls = []
        first = Component("first", calls)
        second = Component("second", calls)
        first.adapter_id = second.adapter_id = "duplicate"
        bot = Bot()
        bot.install(first)
        with self.assertRaises(DuplicateAdapterIdError):
            bot.install(second)

    async def test_drain_stop_failure_does_not_short_circuit_cleanup(self):
        calls = []

        class FailingStop(Component):
            async def stop(self, mode):
                await super().stop(mode)
                raise RuntimeError(f"stop failed: {self.name}")

        bot = Bot()
        service = Component("service", calls)
        first = Component("first", calls)
        failing = FailingStop("failing", calls)
        bot.service(ServiceKey("service"), service)
        bot.install(first)
        bot.install(failing)
        await bot.setup()
        await bot.start()

        report = await bot.stop("drain")

        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)
        self.assertFalse(report.successful)
        self.assertIn("stop:failing:drain", calls)
        self.assertIn("stop:first:drain", calls)
        self.assertIn("stop:service:abort", calls)
        self.assertIn("teardown:service", calls)

    async def test_explicit_lifespan_preserves_body_cancellation(self):
        calls = []
        bot = Bot()

        @asynccontextmanager
        async def extension_lifespan():
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("exit")

        bot.lifespan(extension_lifespan())

        async def run_cancelled_body():
            async with bot.runtime.lifespan():
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await run_cancelled_body()
        self.assertEqual(calls, ["enter", "exit"])
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)

    async def test_stubborn_cleanup_is_reported_as_abandoned(self):
        calls = []

        class Stubborn(Component):
            async def stop(self, mode):
                calls.append(f"stop:{self.name}:{mode}")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await asyncio.Event().wait()

        bot = Bot()
        bot.service(ServiceKey("stubborn"), Stubborn("stubborn", calls))
        await bot.setup()
        await bot.start()

        with patch.object(type(bot.runtime), "COMPONENT_TIMEOUT", 0.01), \
             patch.object(type(bot.runtime), "ABORT_TIMEOUT", 0.03), \
             patch.object(type(bot.runtime), "CANCEL_GRACE", 0.01):
            report = await bot.stop("abort")

        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)
        self.assertIn("stubborn", report.abandoned_components)
        self.assertTrue(any("cleanup:stubborn:stop" in name
                            for name in report.abandoned_tasks))

    async def test_abort_request_escalates_an_inflight_drain(self):
        bot = Bot()
        entered = asyncio.Event()

        @bot.hook()
        async def blocked(ctx):
            entered.set()
            await asyncio.Event().wait()

        await bot.setup()
        await bot.start()
        event = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        draining = asyncio.create_task(bot.stop("drain"))
        await asyncio.sleep(0)

        report = await asyncio.wait_for(bot.stop("abort"), timeout=1.0)
        self.assertIs(report, await draining)
        self.assertTrue(report.forced)
        self.assertEqual(bot.runtime.state, RuntimeState.TERMINATED)
        with self.assertRaises(asyncio.CancelledError):
            await event


if __name__ == "__main__":
    unittest.main()
