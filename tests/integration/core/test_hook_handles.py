"""Hook handle changes and in-flight snapshot stability."""

import asyncio
import unittest

from core import Bot, Envelope


class HookHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_enable_disable_remove_and_lookup(self):
        bot = Bot()
        calls = []

        @bot.hook(name="demo")
        async def demo(ctx):
            calls.append(ctx.raw["value"])

        handle = bot.handle_for(demo)
        self.assertIsNotNone(handle)
        self.assertEqual(handle.name, "demo")
        self.assertEqual(handle.owner, "application")
        self.assertTrue(handle.disable())
        self.assertFalse(handle.disable())
        self.assertFalse(handle.enabled)
        self.assertTrue(handle.enable())

        await bot.setup()
        await bot.start()
        await bot.emit(Envelope("test", {"value": 1}))
        self.assertTrue(handle.remove())
        self.assertFalse(handle.remove())
        await bot.emit(Envelope("test", {"value": 2}))
        await bot.stop()
        self.assertEqual(calls, [1])

    async def test_snapshot_is_stable_for_inflight_dispatch(self):
        bot = Bot()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        @bot.hook(priority=10)
        async def first(ctx):
            entered.set()
            await release.wait()

        @bot.hook()
        async def second(ctx):
            calls.append("second")

        second_handle = bot.handle_for(second)
        await bot.setup()
        await bot.start()
        event = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        second_handle.disable()
        release.set()
        await event
        await bot.emit(Envelope("test", {}))
        await bot.stop()
        self.assertEqual(calls, ["second"])


if __name__ == "__main__":
    unittest.main()
