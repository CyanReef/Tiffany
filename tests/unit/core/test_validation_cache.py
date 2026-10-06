"""Bounded validation caching must preserve platform-specific dependencies."""
import unittest

from core import Bot, Field


class ValidationCacheTests(unittest.TestCase):
    def test_many_platform_names_remain_bounded_and_evicted_names_revalidate(self):
        bot = Bot()
        field = Field("text")
        bot.provide(field, lambda ctx: ctx.raw["text"])

        @bot.hook(needs=(field,))
        async def work(ctx):
            pass

        for index in range(1000):
            bot.validate(f"platform-{index}")
        self.assertLessEqual(len(bot._validated_platforms), 256)
        self.assertFalse(bot._is_validated("platform-0"))
        bot.validate("platform-0")
        self.assertTrue(bot._is_validated("platform-0"))
        self.assertLessEqual(len(bot._validated_platforms), 256)

    def test_eviction_does_not_hide_missing_platform_dependency(self):
        bot = Bot()
        field = Field("text")
        handle = bot.provide(field, lambda ctx: "text", platform="supported")

        @bot.hook(platform="supported", needs=(field,))
        async def work(ctx):
            pass

        bot.validate("supported")
        for index in range(300):
            bot.validate(f"other-{index}")
        handle.revoke()
        with self.assertRaises(LookupError):
            bot.validate("supported")
        self.assertFalse(bot._is_validated("supported"))
