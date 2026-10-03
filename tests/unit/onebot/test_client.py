"""OneBot response correlation and pending-call cleanup."""

import asyncio
import json
import unittest

from clients import (
    OneBotCallTimeoutError,
    OneBotDisconnectedError,
    OneBotPendingLimitError,
    OneBotProtocolError,
    OneBotResponseError,
    OneBotSendError,
    OneBotWebSocketClient,
    SendMessageResult,
)
from core.Metrics import MetricRegistry
from tests.support.onebot import FakeWebSocket


class OneBotClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_echo_requests_correlate_out_of_order_and_return_full_result(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws, generation=7)
        first = asyncio.create_task(client.call("first"))
        second = asyncio.create_task(client.call("second"))
        await asyncio.sleep(0)
        sent = [json.loads(payload) for payload in ws.sent]

        client.handle_response({
            "status": "ok", "retcode": 0, "data": {"value": 2},
            "echo": sent[1]["echo"],
        })
        client.handle_response({
            "status": "ok", "retcode": 0, "data": {"value": 1},
            "echo": sent[0]["echo"],
        })

        first_result, second_result = await asyncio.gather(first, second)
        self.assertEqual(first_result.data, {"value": 1})
        self.assertEqual(second_result.data, {"value": 2})
        self.assertEqual(first_result.action, "first")
        self.assertEqual(first_result.raw["retcode"], 0)
        self.assertEqual(client.pending_count, 0)

    async def test_pending_limit_timeout_cancel_send_failure_and_disconnect_cleanup(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws, pending_limit=1, call_timeout=0.01)
        pending = asyncio.create_task(client.call("held", timeout=1))
        await asyncio.sleep(0)
        with self.assertRaises(OneBotPendingLimitError):
            await client.call("overflow")
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(client.pending_count, 0)

        with self.assertRaises(OneBotCallTimeoutError):
            await client.call("timeout")
        self.assertEqual(client.pending_count, 0)

        ws.send_error = OSError("boom")
        with self.assertRaises(OneBotSendError):
            await client.call("send-failure")
        self.assertEqual(client.pending_count, 0)

        ws.send_error = None
        disconnected = asyncio.create_task(client.call("disconnect", timeout=1))
        await asyncio.sleep(0)
        client.disconnect("test")
        with self.assertRaises(OneBotDisconnectedError):
            await disconnected
        self.assertEqual(client.pending_count, 0)

    async def test_late_response_after_timeout_is_an_orphan(self):
        metrics = MetricRegistry()
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws, call_timeout=0.01, metrics=metrics)
        with self.assertRaises(OneBotCallTimeoutError):
            await client.call("late")
        echo = json.loads(ws.sent[-1])["echo"]
        self.assertTrue(client.handle_response({
            "status": "ok", "retcode": 0, "echo": echo,
        }))
        self.assertEqual(metrics.snapshot().get(
            "onebot_orphan_responses_total",
            {"platform": "onebot", "adapter": "onebot_websocket"},
        ), 1)

    async def test_api_failure_and_reply_result(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws)
        failed = asyncio.create_task(client.call("bad"))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({
            "status": "failed", "retcode": 1404, "wording": "bad action",
            "echo": echo,
        })
        with self.assertRaises(OneBotResponseError) as caught:
            await failed
        self.assertEqual(caught.exception.result.retcode, 1404)

        reply = asyncio.create_task(client.send_text(
            {"message_type": "group", "group_id": 12}, "hello"
        ))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({
            "status": "ok", "retcode": 0, "data": {"message_id": 99},
            "echo": echo,
        })
        result = await reply
        self.assertIsInstance(result, SendMessageResult)
        self.assertEqual(result.message_id, 99)

    async def test_malformed_correlated_response_fails_call(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws)
        call = asyncio.create_task(client.call("bad-response"))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({"echo": echo, "data": {}})
        with self.assertRaises(OneBotProtocolError):
            await call
        self.assertEqual(client.pending_count, 0)

        missing_retcode = asyncio.create_task(client.call("missing-retcode"))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({"status": "ok", "echo": echo, "data": {}})
        with self.assertRaises(OneBotProtocolError):
            await missing_retcode

    async def test_pending_empty_wait_is_event_driven(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws)
        call = asyncio.create_task(client.call("held"))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(client.wait_pending())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({"status": "ok", "retcode": 0, "echo": echo})
        await call
        await waiter

    async def test_response_keeps_pending_slot_until_call_finishes(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(ws, pending_limit=1)
        call = asyncio.create_task(client.call("held"))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        client.handle_response({"status": "ok", "retcode": 0, "echo": echo})

        # The response wakes the caller, while the slot remains owned until
        # call() completes its finally block.
        self.assertEqual(client.pending_count, 1)
        with self.assertRaises(OneBotPendingLimitError):
            await client.call("overflow")
        await call
        self.assertEqual(client.pending_count, 0)

    async def test_timeout_covers_a_blocked_send(self):
        ws = FakeWebSocket()
        ws.gate = asyncio.Event()
        client = OneBotWebSocketClient(ws, call_timeout=0.01)

        with self.assertRaises(OneBotCallTimeoutError):
            await client.call("blocked-send")

        self.assertEqual(client.pending_count, 0)


if __name__ == "__main__":
    unittest.main()
