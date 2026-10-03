"""Hook predicate and handler timeouts and cancellation."""

import asyncio
import unittest

from core import Bot, Envelope, HookTimeoutError


class HookExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_predicate_and_handler_timeouts_and_continue(self):
        bot = Bot()
        calls = []

        async def slow_predicate(ctx):
            await asyncio.sleep(1)
            return True

        @bot.hook(
            name="predicate",
            when=slow_predicate,
            timeout=0.01,
            on_timeout="continue",
        )
        async def predicate(ctx):
            calls.append("never")

        @bot.hook()
        async def next_hook(ctx):
            calls.append("next")

        await bot.setup()
        await bot.start()
        with self.assertLogs("core.Dispatcher", level="WARNING"):
            await bot.emit(Envelope("test", {}))
        await bot.stop()
        self.assertEqual(calls, ["next"])

        aborting = Bot()

        @aborting.hook(name="slow", timeout=0.01)
        async def slow(ctx):
            await asyncio.sleep(1)

        await aborting.setup()
        await aborting.start()
        with self.assertRaises(HookTimeoutError):
            await aborting.emit(Envelope("test", {}))
        await aborting.stop()

    async def test_external_cancel_always_propagates(self):
        bot = Bot()
        entered = asyncio.Event()

        @bot.hook(on_error="continue", on_timeout="continue")
        async def waiting(ctx):
            entered.set()
            await asyncio.Event().wait()

        await bot.setup()
        await bot.start()
        task = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await bot.stop("abort")


if __name__ == "__main__":
    unittest.main()
