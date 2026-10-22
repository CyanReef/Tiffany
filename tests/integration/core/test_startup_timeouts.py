"""Startup deadlines, late acquisitions and bounded cancellation."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import unittest
from unittest.mock import patch

from core import Bot, ComponentStartupTimeoutError, Envelope, Runtime, RuntimeState, ServiceKey, ShutdownReport


class StartupTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def deadlines(self):
        return patch.multiple(Runtime, SETUP_TIMEOUT=.02, START_TIMEOUT=.02,
                              LIFESPAN_TIMEOUT=.02, CANCEL_GRACE=.02, ABORT_TIMEOUT=.2)

    async def test_existing_close_cleans_a_settled_failed_startup(self):
        calls = []
        class Service:
            async def teardown(self):
                calls.append("released")
        bot = Bot()
        bot.service(ServiceKey("retained"), Service())
        await bot.setup()
        # A critical close can already be waiting for the startup lock when
        # late startup finishes. It must perform the retry, not return stale data.
        runtime = bot.runtime
        runtime.state, runtime._failed_start, runtime._startup_retry = RuntimeState.STOP_FAILED, True, True
        runtime.shutdown_report = ShutdownReport(forced=True, abandoned_tasks=("late-startup",))
        runtime._close_task = runtime.tasks.spawn(runtime._close(), name="runtime:critical-abort",
                                                  owner="runtime-close", critical=False)
        report = await bot.stop("abort")
        self.assertTrue(report.successful)
        self.assertEqual(calls, ["released"])
        self.assertFalse(runtime._startup_retry)
        self.assertEqual(runtime.state, RuntimeState.STOP_FAILED)

    async def test_swallowed_cancellation_is_timeout_and_late_setup_is_cleaned(self):
        calls = []
        class Service:
            async def setup(self, runtime):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    calls.append("acquired")
            async def teardown(self):
                calls.append("released")
        bot = Bot()
        bot.service(ServiceKey("slow"), Service())
        with self.deadlines(), self.assertRaises(ComponentStartupTimeoutError):
            await bot.setup()
        self.assertEqual(calls, ["acquired", "released"])
        self.assertEqual(bot.runtime.startup_results[0].outcome, "timeout")
        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)

    async def test_component_timeout_error_is_an_error_not_a_deadline(self):
        cause = TimeoutError("downstream timeout")
        class Service:
            async def setup(self, runtime):
                raise cause
        bot = Bot()
        bot.service(ServiceKey("error"), Service())
        with self.assertRaises(TimeoutError) as raised:
            await bot.setup()
        self.assertIs(raised.exception, cause)
        self.assertEqual(bot.runtime.startup_results[0].outcome, "error")

    async def test_stubborn_start_retains_dependencies_until_explicit_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
        calls = []
        class Dependency:
            async def teardown(self):
                calls.append("dependency")
        class Stubborn:
            async def start(self):
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                calls.append("started")
            async def stop(self, mode):
                calls.append("stopped")
                cleanup_entered.set()
                await cleanup_release.wait()
            async def teardown(self):
                calls.append("teardown")
        bot = Bot()
        dependency = ServiceKey("dependency")
        bot.service(dependency, Dependency())
        bot.service(ServiceKey("stubborn"), Stubborn(), dependencies=(dependency,))
        try:
            with self.deadlines(), self.assertRaises(ComponentStartupTimeoutError):
                await asyncio.wait_for(bot.start(), 1)
            self.assertTrue(entered.is_set())
            self.assertEqual(calls, [])
            report = await bot.stop("abort")
            self.assertFalse(report.successful)
            self.assertIn("Stubborn", report.abandoned_components)
            self.assertTrue(any(name.startswith("startup:") for name in report.abandoned_tasks))
            with self.assertRaisesRegex(RuntimeError, "startup resources are retained"):
                await bot.application.unload(mode="abort")
            self.assertEqual(calls, [])
        finally:
            release.set()
            pending = tuple(bot.runtime._startup_pending)
            if pending:
                await asyncio.wait(pending, timeout=1)
            first_waiter = asyncio.create_task(bot.stop("abort"))
            await asyncio.wait_for(cleanup_entered.wait(), 1)
            second_waiter = asyncio.create_task(bot.stop("abort"))
            closed_waiter = asyncio.create_task(bot.runtime.wait_closed())
            await asyncio.sleep(0)
            try:
                self.assertFalse(first_waiter.done())
                self.assertFalse(second_waiter.done())
                self.assertFalse(closed_waiter.done())
            finally:
                cleanup_release.set()
                first, second, closed = await asyncio.gather(first_waiter, second_waiter, closed_waiter)
        self.assertIs(first, second)
        self.assertIs(first, closed)
        self.assertTrue(first.successful)
        self.assertEqual(calls, ["started", "stopped", "teardown", "dependency"])
        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)
        await bot.stop("abort")
        self.assertEqual(len(calls), 4)
        await bot.application.unload(mode="abort")
        self.assertEqual(len(calls), 4)
        with self.assertRaises(RuntimeError):
            await bot.start()

    async def test_total_budget_and_external_cancellation(self):
        for phase in ("setup", "start"):
            with self.subTest(phase=phase):
                calls = []
                class Service:
                    async def setup(self, runtime):
                        if phase == "setup":
                            await asyncio.sleep(.015)
                    async def start(self):
                        if phase == "start":
                            await asyncio.sleep(.015)
                    async def teardown(self):
                        calls.append("cleaned")
                bot = Bot()
                bot.service(ServiceKey("one"), Service())
                bot.service(ServiceKey("two"), Service())
                with patch.object(Runtime, "STARTUP_TIMEOUT", .02), self.assertRaises(ComponentStartupTimeoutError):
                    await getattr(bot, phase)()
                self.assertEqual(calls, ["cleaned"] * (1 if phase == "setup" else 2))
        entered = asyncio.Event()
        class Cancelled:
            async def setup(self, runtime):
                entered.set()
                await asyncio.Event().wait()
        bot = Bot()
        bot.service(ServiceKey("cancelled"), Cancelled())
        task = asyncio.create_task(bot.setup())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(bot.runtime.state, RuntimeState.STOP_FAILED)
        self.assertEqual(bot.runtime.tasks.active_count, 0)

    async def test_lifecycle_context_survives_enter_exit_without_leaking_to_host(self):
        marker = ContextVar("lifecycle-context", default="host")
        seen = []
        @asynccontextmanager
        async def lifespan():
            token = marker.set("runtime")
            try:
                yield
            finally:
                seen.append(marker.get())
                marker.reset(token)
        bot = Bot()
        bot.lifespan(lifespan())
        @bot.hook()
        async def read(ctx):
            seen.append(marker.get())
            marker.set("event")
        async with bot:
            self.assertEqual(marker.get(), "host")
            await bot.emit(Envelope("test", {}))
            await bot.emit(Envelope("test", {}))
        self.assertEqual(seen, ["runtime", "runtime", "runtime"])
        self.assertEqual(marker.get(), "host")

    async def test_late_start_and_lifespan_acquisitions_are_rolled_back(self):
        for phase in ("start", "enter"):
            with self.subTest(phase=phase):
                calls = []
                async def acquire():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        calls.append("acquired")
                class Service:
                    async def start(self):
                        await acquire()
                    async def stop(self, mode):
                        calls.append("released")
                @asynccontextmanager
                async def lifespan():
                    await acquire()
                    try:
                        yield
                    finally:
                        calls.append("released")
                bot = Bot()
                if phase == "start":
                    bot.service(ServiceKey("slow-start"), Service())
                else:
                    bot.lifespan(lifespan())
                with self.deadlines(), self.assertRaises(ComponentStartupTimeoutError):
                    await bot.start()
                self.assertEqual(calls, ["acquired", "released"])
                self.assertEqual(bot.runtime.startup_results[-1].outcome, "timeout")
