"""Transport frame failures isolated from the real hook runtime."""

import json
import unittest

from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from adapters.onebot_fields import register_onebot_fields
from core import Bot
from hooks import register_hooks
from tests.support.async_helpers import run
from tests.support.onebot import FakeIncomingWebSocket


class AdapterFrameTests(unittest.TestCase):
    def test_bad_frame_and_bad_hook_do_not_block_next_frame(self):
        bot = Bot()
        register_onebot_fields(bot, "napcat")
        register_hooks(bot)
        failures = 0

        @bot.hook(on="message", priority=100)
        async def fail_once(ctx):
            nonlocal failures
            failures += 1
            if failures == 1:
                raise RuntimeError("boom")

        adapter = OneBotWebSocketAdapter(
            "127.0.0.1", 6199, "napcat", workers=1
        )
        bot.install(adapter)
        ws = FakeIncomingWebSocket([
            "not json",
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "message": "ping",
                "group_id": 123,
                "user_id": 456,
                "self_id": 789,
            }),
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "message": "ping",
                "group_id": 123,
                "user_id": 456,
                "self_id": 789,
            }),
        ])

        with self.assertLogs(
            "adapters.OneBotWebSocketAdapter",
            level="ERROR",
        ) as logs:
            async def dispatch_frames():
                async with bot:
                    await adapter.handle(ws)

            run(dispatch_frames())

        self.assertGreaterEqual(len(logs.output), 1)
        # The first hook failure is isolated from transport reading; the next
        # frame is still admitted and reaches the hook runtime.
        self.assertEqual(failures, 2)


if __name__ == "__main__":
    unittest.main()
