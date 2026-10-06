"""Hook handle changes and in-flight snapshot stability."""

import asyncio
import gc
import unittest
import weakref

from core import Bot, Context, Envelope


class HookHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_hook_releases_handler_after_last_external_reference(self):
        bot = Bot()

        class Handler:
            async def __call__(self, ctx):
                pass

        handler = Handler()
        reference = weakref.ref(handler)
        handle = bot.register_hook(handler, name="temporary")
        del handler
        self.assertTrue(handle.remove())
        self.assertEqual(bot.dispatcher.handles(), ())
        self.assertTrue(handle.removed)
        self.assertFalse(handle.enable())
        self.assertFalse(handle.disable())
        self.assertFalse(handle.remove())
        self.assertIs(handle.hook.handle, reference())
        del handle
        gc.collect()
        self.assertIsNone(reference())

    async def test_bulk_removal_releases_handlers_but_keeps_snapshot_executable(self):
        bot = Bot()
        calls = []

        class Handler:
            async def __call__(self, ctx):
                calls.append(ctx.raw["value"])

        handler = Handler()
        reference = weakref.ref(handler)
        scope = bot.scope("temporary")
        scope.register_hook(handler)
        snapshot = bot.dispatcher.snapshot()
        del handler
        self.assertEqual(bot.dispatcher.remove_owner(scope.owner), 1)
        self.assertEqual(bot.dispatcher.handles(), ())
        await bot.dispatcher.dispatch(
            Context(Envelope("test", {"value": 7}), bot.providers),
            snapshot=snapshot,
        )
        self.assertEqual(calls, [7])
        del snapshot
        gc.collect()
        self.assertIsNone(reference())

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
