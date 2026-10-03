"""Ping replies, filtering and lazy field use through OneBot."""

import asyncio
import json
import unittest

from adapters.onebot_fields import detect_event_kind, register_onebot_fields
from clients.OneBotWebSocketClient import OneBotWebSocketClient
from core import Bot, Envelope
from fields import SELF_ID, TEXT, USER_ID
from hooks import register_hooks
from hooks.command import COMMAND
from tests.support.async_helpers import emit_running, run
from tests.support.onebot import RecordingWebSocket as FakeWebSocket


class PingTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        register_onebot_fields(self.bot, "napcat")
        register_hooks(self.bot)

    def test_ping_replies_once_in_group(self):
        class EchoWebSocket(FakeWebSocket):
            def __init__(self):
                super().__init__()
                self.client = None

            async def send(self, payload: str) -> None:
                await super().send(payload)
                request = json.loads(payload)
                asyncio.get_running_loop().call_soon(
                    self.client.handle_response,
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 1},
                        "echo": request["echo"],
                    },
                )

        ws = EchoWebSocket()
        client = OneBotWebSocketClient(ws)
        ws.client = client
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message": "ping",
            "group_id": 123,
            "user_id": 456,
            "self_id": 789,
        }

        run(emit_running(self.bot, Envelope(
            "napcat",
            raw,
            client=client,
            kind=detect_event_kind(raw),
        )))

        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(json.loads(ws.sent[0]), {
            "action": "send_msg",
            "params": {
                "message_type": "group",
                "message": "pong",
                "group_id": 123,
            },
            "echo": "0:1",
        })

    def test_plain_notice_and_self_message_do_not_reply(self):
        samples = [
            {
                "post_type": "message",
                "message_type": "private",
                "message": "hello",
                "user_id": 1,
                "self_id": 2,
            },
            {"post_type": "notice", "message": "ping"},
            {
                "post_type": "message",
                "message_type": "group",
                "message": "ping",
                "user_id": 9,
                "self_id": 9,
                "group_id": 10,
            },
        ]
        samples.extend({
            "post_type": "message",
            "message_type": "private",
            "message": text,
            "user_id": 1,
            "self_id": 2,
        } for text in ("/ping", "pingpong", "hello ping", "ping hello"))

        async def dispatch_all():
            async with self.bot:
                results = []
                for raw in samples:
                    ws = FakeWebSocket()
                    await self.bot.emit(Envelope(
                        "napcat",
                        raw,
                        client=OneBotWebSocketClient(ws),
                        kind=detect_event_kind(raw),
                    ))
                    results.append(ws.sent)
                return results

        for raw, sent in zip(samples, run(dispatch_all()), strict=True):
            with self.subTest(raw=raw):
                self.assertEqual(sent, [])

    def test_plain_message_only_resolves_text(self):
        raw = {
            "post_type": "message",
            "message": "hello",
            "user_id": 1,
            "self_id": 2,
        }
        ctx = run(emit_running(
            self.bot,
            Envelope("napcat", raw, kind="message"),
        ))

        self.assertFalse(ctx.has(COMMAND))
        self.assertTrue(ctx.has(TEXT))
        self.assertFalse(ctx.has(USER_ID))
        self.assertFalse(ctx.has(SELF_ID))


if __name__ == "__main__":
    unittest.main()
