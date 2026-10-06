"""Adapter construction and ingress using an isolated runtime stub."""

import asyncio
import json
import unittest
from unittest.mock import patch

from adapters import create_adapter
from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from clients import OneBotWebSocketClient
from core import Bot, Envelope
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
            "127.0.0.1", 6199, "napcat", workers=2
        )
        self.assertEqual(adapter.workers, 2)
        self.assertFalse(hasattr(adapter, "queue_size"))

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
            "127.0.0.1", 6199, "napcat"
        )
        await self.adapter.setup(self.runtime)
        self.adapter._accepting_events = True
        self.adapter._started = True

    async def asyncTearDown(self):
        await self.runtime.tasks.close()

    async def test_synchronous_admission_without_submission_task(self):
        envelope = Envelope("napcat", {})
        for _ in range(10):
            self.adapter._submit_event(envelope)
        self.assertEqual(self.runtime.envelopes, [envelope] * 10)
        self.assertEqual(self.runtime.tasks.snapshot(), ())

    async def test_buffered_frames_yield_to_control_task_every_64_frames(self):
        counts = []
        ws = FakeWebSocket(incoming=[json.dumps({'post_type': 'message'})] * 128)
        async def control():
            counts.append(len(self.runtime.envelopes))
            await asyncio.sleep(0)
            counts.append(len(self.runtime.envelopes))
        observer = asyncio.create_task(control())
        await self.adapter.handle(ws)
        await observer
        self.assertEqual(counts, [64, 128])
        self.assertEqual(len(self.runtime.envelopes), 128)

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

    async def test_rejection_is_counted_and_log_is_rate_limited(self):
        self.runtime.emit_result = None
        with self.assertLogs("adapters.OneBotWebSocketAdapter", level="WARNING") as logs:
            for _ in range(10):
                self.adapter._submit_event(Envelope("napcat", {}))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.runtime.metrics.snapshot().get(
            "onebot_events_dropped_total", {"platform": "napcat", "adapter": self.adapter.adapter_id, "reason": "overload"}), 10)

    async def test_event_metadata_session_and_charge(self):
        client = OneBotWebSocketClient(FakeWebSocket())
        raw = {
            "post_type": "message", "message_type": "group",
            "group_id": 42, "self_id": 7,
        }
        await self.adapter._process_frame(json.dumps(raw), client, "connection-1")
        await self.adapter._process_frame(json.dumps(raw), client, "connection-1")
        await asyncio.sleep(0)

        self.assertEqual(len(self.runtime.envelopes), 2)
        envelope = self.runtime.envelopes[0]
        self.assertEqual(envelope.connection_id, "connection-1")
        self.assertEqual(envelope.session_id, "7:group:42")
        self.assertEqual(envelope.adapter_id, self.adapter.adapter_id)
        self.assertGreater(envelope.admission_bytes, 2048)
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
