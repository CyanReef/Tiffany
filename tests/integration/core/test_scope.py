"""Scope dependencies, in-flight ownership and resource cleanup."""

import asyncio
import unittest
from contextlib import asynccontextmanager

from core import Bot, Envelope, Field, OwnerInUseError, ServiceKey


class Service:
    def __init__(self):
        self.calls = []

    async def setup(self, runtime):
        self.calls.append("setup")

    async def start(self):
        self.calls.append("start")

    async def stop(self, mode):
        self.calls.append(f"stop:{mode}")

    async def teardown(self):
        self.calls.append("teardown")


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hook_uses_is_validated_when_registered(self):
        bot = Bot()
        missing = ServiceKey("missing")
        with self.assertRaisesRegex(LookupError, "missing"):
            bot.scope("extension.invalid").register_hook(
                _noop,
                uses=(missing,),
            )

    async def test_external_dependency_blocks_atomic_unload(self):
        bot = Bot()
        field = Field("owned")
        owner = bot.scope("extension.owner")
        owner.provide(field, lambda ctx: "value")

        @bot.hook(needs=(field,))
        async def consumer(ctx):
            ctx.resolve(field)

        provider = bot.providers.handles_for_owner(owner.owner)[0]
        with self.assertRaises(OwnerInUseError):
            await owner.unload()
        self.assertTrue(provider.active)
        self.assertTrue(bot.handle_for(consumer).active)

    async def test_unload_waits_for_captured_snapshot_and_leaves_no_residue(self):
        bot = Bot()
        scope = bot.scope("extension.weather")
        service_key = ServiceKey[Service]("weather.http")
        service = Service()
        scope.service(service_key, service)
        entered = asyncio.Event()
        release = asyncio.Event()

        @scope.hook(uses=(service_key,))
        async def weather(ctx):
            self.assertIs(ctx.service(service_key), service)
            entered.set()
            await release.wait()

        await bot.setup()
        await bot.start()
        event = asyncio.create_task(bot.emit(Envelope("test", {})))
        await entered.wait()
        unload = asyncio.create_task(scope.unload())
        await asyncio.sleep(0)
        self.assertFalse(unload.done())
        release.set()
        await event
        await unload
        self.assertEqual(bot.dispatcher.handles_for_owner(scope.owner), ())
        self.assertEqual(bot.providers.handles_for_owner(scope.owner), ())
        self.assertEqual(bot.services.handles_for_owner(scope.owner), ())
        self.assertEqual(service.calls[-2:], ["stop:drain", "teardown"])
        await scope.unload()
        self.assertEqual(service.calls[-2:], ["stop:drain", "teardown"])
        await bot.stop()

    async def test_unload_does_not_wait_for_unrelated_event_route(self):
        bot = Bot()
        unused = bot.scope("extension.unused")
        active = bot.scope("extension.active")
        entered = asyncio.Event()
        release = asyncio.Event()

        @unused.hook(on="notice")
        async def notice(ctx):
            pass

        @active.hook(on="message")
        async def message(ctx):
            entered.set()
            await release.wait()

        await bot.start()
        event = asyncio.create_task(bot.emit(Envelope("test", {}, kind="message")))
        try:
            await entered.wait()
            await asyncio.wait_for(unused.unload(), timeout=1.0)
            self.assertFalse(event.done())
            self.assertEqual(bot.dispatcher.handles_for_owner(unused.owner), ())
        finally:
            release.set()
            await event
            await bot.stop()

    async def test_unhashable_owner_and_owned_lifespan_unload_cleanly(self):
        bot = Bot()
        owner = []
        scope = bot.scope("extension.custom")
        scope.owner = owner
        calls = []

        @asynccontextmanager
        async def lifespan():
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("exit")

        scope.lifespan(lifespan())

        @scope.hook()
        async def owned(ctx):
            calls.append("hook")

        await bot.setup()
        await bot.start()
        await bot.emit(Envelope("test", {}))
        await scope.unload()

        self.assertEqual(calls, ["enter", "hook", "exit"])
        self.assertEqual(bot.dispatcher.handles_for_owner(owner), ())
        self.assertEqual(bot.runtime.lifespans_for_owner(owner), ())
        self.assertEqual(bot.runtime.tasks.tasks_for(owner), ())
        await bot.stop()

    async def test_hot_hook_change_invalidates_validation_revision(self):
        bot = Bot()
        await bot.setup()
        await bot.start()
        await bot.emit(Envelope("test", {}))
        missing = ServiceKey("late.missing")

        # A direct low-level mutation must still invalidate the revision-based
        # validation cache on the next event.
        bot.dispatcher.add(_hook_using(missing))
        with self.assertRaisesRegex(LookupError, "late.missing"):
            await bot.emit(Envelope("test", {}))
        await bot.stop("abort")


async def _noop(ctx):
    pass


def _hook_using(service_key):
    from core import Hook

    return Hook(name="late", handle=_noop, uses=(service_key,))


if __name__ == "__main__":
    unittest.main()
