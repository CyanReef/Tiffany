"""OneBot field extraction and event-kind detection."""

import unittest

from adapters.onebot_fields import detect_event_kind, register_onebot_fields
from core import Bot, Context, Envelope
from fields import TEXT


class OneBotFieldTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        register_onebot_fields(self.bot, "napcat")

    def context(self, raw):
        return Context(Envelope("napcat", raw), self.bot.providers)

    def test_text_supports_string_and_mixed_segments(self):
        self.assertEqual(self.context({"message": "hello"}).resolve(TEXT), "hello")
        self.assertEqual(
            self.context({
                "message": [
                    {"type": "text", "data": {"text": "hello"}},
                    {"type": "image", "data": {"file": "x"}},
                    {"type": "text", "data": {"text": " world"}},
                    "invalid",
                ]
            }).resolve(TEXT),
            "hello world",
        )

    def test_event_kind_distinguishes_responses(self):
        self.assertEqual(detect_event_kind({"post_type": "message"}), "message")
        self.assertEqual(detect_event_kind({"status": "ok", "retcode": 0}), "response")
        self.assertEqual(detect_event_kind({"meta": True}), "event")


if __name__ == "__main__":
    unittest.main()
