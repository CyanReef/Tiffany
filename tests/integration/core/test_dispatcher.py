"""Routing, priority, validation and error policy through Bot."""

import unittest

from core import Bot, Envelope, Field, HookExecutionError
from tests.support.async_helpers import emit_running, run


class DispatcherTests(unittest.TestCase):
    def test_on_priority_when_needs_and_stop(self):
        field = Field[str]("test.value")
        bot = Bot()
        provider_calls = 0
        calls: list[str] = []

        def provide(ctx):
            nonlocal provider_calls
            provider_calls += 1
            return "value"

        bot.provide(field, provide)

        @bot.hook(name="notice", on="notice", needs=(field,), priority=100)
        async def notice(ctx):
            calls.append("notice")

        @bot.hook(name="rejected", on="message", needs=(field,), priority=20,
                  when=lambda ctx: False)
        async def rejected(ctx):
            calls.append("rejected")

        @bot.hook(name="first", on="message", needs=(field,), priority=10)
        async def first(ctx):
            calls.append(ctx.resolve(field))
            ctx.stop()

        @bot.hook(name="last", on="message", priority=0)
        async def last(ctx):
            calls.append("last")

        ctx = run(emit_running(bot, Envelope("test", {}, kind="message")))

        self.assertEqual(calls, ["value"])
        self.assertEqual(provider_calls, 1)
        self.assertTrue(ctx.stopped)

    def test_same_priority_keeps_registration_order(self):
        bot = Bot()
        calls: list[str] = []

        @bot.hook()
        async def first(ctx):
            calls.append("first")

        @bot.hook()
        async def second(ctx):
            calls.append("second")

        run(emit_running(bot, Envelope("test", {})))

        self.assertEqual(calls, ["first", "second"])

    def test_needs_validates_without_eager_resolution(self):
        field = Field[str]("lazy")
        bot = Bot()
        provider_calls = 0

        def provide(ctx):
            nonlocal provider_calls
            provider_calls += 1
            return "value"

        bot.provide(field, provide)

        @bot.hook(needs=(field,))
        async def hook(ctx):
            pass

        run(emit_running(bot, Envelope("test", {})))

        self.assertEqual(provider_calls, 0)

    def test_unknown_event_kinds_share_one_fallback_route(self):
        bot = Bot()

        @bot.hook(on="message")
        async def message(ctx):
            pass

        async def dispatch_all():
            async with bot:
                for index in range(100):
                    await bot.emit(Envelope("test", {}, kind=f"unknown-{index}"))

        run(dispatch_all())

        self.assertEqual(len(bot.dispatcher._route_cache), 1)

    def test_hook_error_policy_names_failure_and_can_continue(self):
        aborting = Bot()

        @aborting.hook(name="broken")
        async def broken(ctx):
            raise RuntimeError("boom")

        with self.assertRaisesRegex(HookExecutionError, "broken"):
            run(emit_running(aborting, Envelope("test", {})))

        continuing = Bot()
        calls: list[str] = []

        @continuing.hook(name="optional", on_error="continue")
        async def optional(ctx):
            raise RuntimeError("boom")

        @continuing.hook()
        async def next_hook(ctx):
            calls.append("next")

        with self.assertLogs("core.Dispatcher", level="ERROR") as logs:
            run(emit_running(continuing, Envelope("test", {})))

        self.assertIn("optional", logs.output[0])
        self.assertEqual(calls, ["next"])

    def test_platform_scoped_needs_only_validate_for_that_platform(self):
        field = Field[str]("napcat.only")
        bot = Bot()
        bot.provide(field, lambda ctx: "value", platform="napcat")

        @bot.hook(platform="napcat", needs=(field,))
        async def napcat_only(ctx):
            pass

        bot.validate("napcat")
        bot.validate("telegram")

        run(emit_running(bot, Envelope("telegram", {})))


if __name__ == "__main__":
    unittest.main()
