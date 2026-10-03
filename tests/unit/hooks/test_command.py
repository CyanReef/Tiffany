"""Command parsing from the lazy text provider."""

import unittest

from adapters.onebot_fields import register_onebot_fields
from core import Bot, Context, Envelope
from hooks import register_hooks
from hooks.command import COMMAND, Command


class CommandProviderTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        register_onebot_fields(self.bot, "napcat")
        register_hooks(self.bot)

    def test_command_is_derived_from_text(self):
        ctx = Context(
            Envelope("napcat", {"message": " /EcHo hello world "}),
            self.bot.providers,
        )

        self.assertEqual(
            ctx.resolve(COMMAND),
            Command(name="echo", args=("hello", "world")),
        )


if __name__ == "__main__":
    unittest.main()
