"""Adapter construction and ingress using an isolated runtime stub."""

import asyncio
import json
import unittest

from adapters import create_adapter
from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from clients import OneBotWebSocketClient
from core import Bot
from hooks import register_hooks
from settings import AdapterConfig, WebSocketConfig
from tests.support.onebot import FakeRuntime, FakeWebSocket


class AdapterConfigurationTests(unittest.TestCase):
    def test_adapter_factory_is_idempotent_for_one_bot(self):
        bot = Bot()
        register_hooks(bot)
        config = AdapterConfig(
            type="onebot_websocket",
            platform="napcat",
            websocket=WebSocketConfig(host="127.0.0.1", port=6199),
        )

        first = create_adapter(config)
        second = create_adapter(config)
        self.assertIsNone(first.runtime)
        self.assertIsNone(second.runtime)

    def test_non_object_json_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            OneBotWebSocketAdapter._decode_event("[]")

    def test_queue_is_bounded_and_workers_process_concurrently(self):
        # Runtime limits and fairness are covered by integration/core/test_scheduler.py.
        # Construction preserves the configured adapter limits.
        adapter = OneBotWebSocketAdapter(
            "127.0.0.1", 6199, "napcat", workers=2, queue_size=1
        )
        self.assertEqual(adapter.workers, 2)
        self.assertEqual(adapter.queue_size, 1)

    def test_factory_new_signature_is_side_effect_free(self):
        config = AdapterConfig(
            type="onebot_websocket",
            platform="napcat",
            websocket=WebSocketConfig(host="127.0.0.1", port=6199),
        )
        adapter = create_adapter(config)
        self.assertIsNone(adapter.runtime)
        self.assertIsNone(adapter._server)


class AdapterIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = FakeRuntime()
        self.adapter = OneBotWebSocketAdapter(
            "127.0.0.1", 6199, "napcat", queue_size=1
        )
        await self.adapter.setup(self.runtime)
        self.adapter._accepting_events = True
        self.adapter._started = True

    async def asyncTearDown(self):
        await self.runtime.tasks.close()

    async def test_echo_is_never_emitted_and_orphan_is_counted(self):
        ws = FakeWebSocket()
        client = OneBotWebSocketClient(
            ws, metrics=self.runtime.metrics, platform="napcat",
            adapter_id=self.adapter.adapter_id,
        )
        await self.adapter._process_frame(json.dumps({
            "status": "ok", "retcode": 0, "echo": "missing",
        }), client, "connection-1")
        await asyncio.sleep(0)

        self.assertEqual(self.runtime.envelopes, [])
        self.assertEqual(
            self.runtime.metrics.snapshot().get(
                "onebot_orphan_responses_total",
                {"platform": "napcat", "adapter": self.adapter.adapter_id},
            ),
            1,
        )

    async def test_runtime_emit_fallback_uses_non_waiting_admission(self):
        class CurrentRuntime(FakeRuntime):
            def __init__(self):
                super().__init__()
                self.emit_calls = []

            async def emit(self, envelope, *, reject, wait):
                self.emit_calls.append((envelope, reject, wait))
                return object()

        runtime = CurrentRuntime()
        adapter = OneBotWebSocketAdapter("127.0.0.1", 6199, "napcat")
        await adapter.setup(runtime)
        adapter._accepting_events = True
        # Shadow the inherited fake API so the adapter exercises Runtime.emit.
        runtime.emit_from_adapter = None
        client = OneBotWebSocketClient(FakeWebSocket())
        await adapter._process_frame(
            json.dumps({"post_type": "notice"}), client, "connection-1"
        )
        await asyncio.sleep(0)
        self.assertEqual(len(runtime.emit_calls), 1)
        self.assertEqual(runtime.emit_calls[0][1:], (False, False))
        await runtime.tasks.close()

    async def test_event_metadata_session_and_drop_newest(self):
        self.runtime.emit_gate = asyncio.Event()
        client = OneBotWebSocketClient(FakeWebSocket())
        raw = {
            "post_type": "message", "message_type": "group",
            "group_id": 42, "self_id": 7,
        }
        await self.adapter._process_frame(json.dumps(raw), client, "connection-1")
        await self.adapter._process_frame(json.dumps(raw), client, "connection-1")
        await asyncio.sleep(0)

        self.assertEqual(len(self.runtime.envelopes), 1)
        envelope = self.runtime.envelopes[0]
        self.assertEqual(envelope.connection_id, "connection-1")
        self.assertEqual(envelope.session_id, "7:group:42")
        self.assertEqual(envelope.adapter_id, self.adapter.adapter_id)
        self.runtime.emit_gate.set()
        await asyncio.sleep(0)

    async def test_single_active_connection_rejects_second_with_1013(self):
        first = FakeWebSocket()
        second = FakeWebSocket()
        first_gate = asyncio.Event()

        async def hold_first():
            self.adapter._active_ws = first
            await first_gate.wait()

        holder = asyncio.create_task(hold_first())
        await asyncio.sleep(0)
        await self.adapter.handle(second)
        self.assertEqual(second.closed[0][0], 1013)
        first_gate.set()
        await holder
        self.adapter._active_ws = None

    async def test_connection_generations_are_monotonic_and_ids_are_distinct(self):
        first = FakeWebSocket()
        await self.adapter.handle(first)
        first_generation = self.adapter.connection_generation
        second = FakeWebSocket()
        await self.adapter.handle(second)
        self.assertEqual(first_generation, 1)
        self.assertEqual(self.adapter.connection_generation, 2)


if __name__ == "__main__":
    unittest.main()
