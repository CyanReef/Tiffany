import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("install the qqofficial extra to test its adapter")

from adapters.QQOfficialWebSocketAdapter import QQOfficialGatewayError, QQOfficialWebSocketAdapter, _Reconnect
from core import Bot, MetricRegistry, RuntimeState, TaskRegistry
from fields import TEXT
from tests.support.qqofficial import message


class QQOfficialAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = SimpleNamespace(
            bot=Bot(), state=RuntimeState.RUNNING, tasks=TaskRegistry(), metrics=MetricRegistry(),
            scheduler=SimpleNamespace(configure_adapter=Mock(), wait_adapter_idle=AsyncMock()), submit=Mock(return_value=object()),
        )
        self.adapter = QQOfficialWebSocketAdapter("app", "secret", dedup_limit=2)
        self.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        await self.adapter.setup(self.runtime)

    async def asyncTearDown(self):
        await self.adapter.teardown()
        await self.runtime.tasks.close()

    async def test_admission_preserves_payload_and_uses_scene_specific_session_keys(self):
        group = message(target="same")
        await self.adapter._handle(group, self.ws)
        envelope = self.runtime.submit.call_args.args[0]
        self.assertIs(envelope.raw, group)
        self.assertIs(envelope.client, self.adapter.client)
        self.assertEqual(envelope.event_id, "event-2")
        self.assertEqual(envelope.session_id, "app:group:same")
        self.assertEqual(self.runtime.submit.call_args.kwargs, {"reject": False})
        await self.adapter._handle(message("private", seq=3, target="same"), self.ws)
        self.assertEqual(self.runtime.submit.call_args.args[0].session_id, "app:private:same")

    async def test_rejected_message_is_not_deduplicated_or_checkpointed(self):
        self.adapter._admitted_seq = 1
        self.runtime.submit.return_value = None
        raw = message(seq=2)
        with self.assertRaises(_Reconnect):
            await self.adapter._handle(raw, self.ws)
        self.assertEqual(self.adapter._received_seq, 2)
        self.assertEqual(self.adapter._admitted_seq, 1)
        self.assertFalse(self.adapter._seen)
        self.runtime.submit.return_value = object()
        await self.adapter._handle(raw, self.ws)
        await self.adapter._handle(message(seq=3, message_id="message-2"), self.ws)
        self.assertEqual(self.runtime.submit.call_count, 2)
        self.assertEqual(self.adapter._admitted_seq, 3)

    async def test_adapter_drain_preserves_client_until_queued_work_finishes(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        async def wait(adapter_id):
            self.assertEqual(adapter_id, self.adapter.adapter_id)
            entered.set()
            await gate.wait()
        self.runtime.scheduler.wait_adapter_idle.side_effect = wait
        self.adapter._ws = self.ws
        self.adapter.client.stop = AsyncMock()
        stopping = asyncio.create_task(self.adapter.stop('drain'))
        await entered.wait()
        self.ws.close.assert_not_awaited()
        self.adapter.client.stop.assert_not_awaited()
        gate.set()
        await stopping
        self.ws.close.assert_awaited_once()
        self.adapter.client.stop.assert_awaited_once_with('drain')

    async def test_dedup_expiry_and_capacity_are_bounded(self):
        with patch("adapters.QQOfficialWebSocketAdapter.time.monotonic", return_value=10):
            for seq in (2, 3, 4):
                await self.adapter._handle(message(seq=seq), self.ws)
        self.assertEqual(len(self.adapter._seen), 2)
        with patch("adapters.QQOfficialWebSocketAdapter.time.monotonic", return_value=3611):
            await self.adapter._handle(message(seq=3), self.ws)
        self.assertEqual(self.runtime.submit.call_count, 4)
        self.assertEqual(len(self.adapter._seen), 1)

    async def test_control_frames_stay_inside_adapter_and_invalid_session_clears_resume(self):
        await self.adapter._handle({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session", "user": {"id": "bot-id"}}}, self.ws)
        self.assertEqual(self.adapter.self_id, "bot-id")
        self.assertEqual(self.adapter._session_id, "session")
        await self.adapter._handle({"op": 1}, self.ws)
        self.assertTrue(self.adapter._awaiting_ack)
        await self.adapter._handle({"op": 11}, self.ws)
        self.assertFalse(self.adapter._awaiting_ack)
        with self.assertRaises(_Reconnect) as raised:
            await self.adapter._handle({"op": 9, "d": False}, self.ws)
        self.assertTrue(raised.exception.clear_session)
        self.runtime.submit.assert_not_called()

    async def test_start_waits_for_runtime_and_rolls_back_providers_on_teardown(self):
        self.runtime.state = RuntimeState.STARTING
        self.adapter.client.request = AsyncMock(return_value={"url": "ws://unused.invalid"})
        self.adapter._connection = AsyncMock()
        await self.adapter.start()
        await asyncio.sleep(0.025)
        self.adapter._connection.assert_not_awaited()
        await self.adapter.teardown()
        self.assertFalse(self.runtime.tasks.failures)
        with self.assertRaises(LookupError):
            self.runtime.bot.providers.get(TEXT, "qq_official")

    async def test_lifecycle_gate_never_checkpoint_events_during_drain(self):
        self.adapter._admitted_seq = 1
        self.runtime.state = RuntimeState.STOPPING_DRAIN
        with self.assertRaises(_Reconnect):
            await self.adapter._handle(message(), self.ws)
        self.assertEqual(self.adapter._admitted_seq, 1)
        self.runtime.submit.assert_not_called()

    async def test_heartbeat_ack_timeout_closes_connection(self):
        self.adapter._ws = self.ws
        await self.adapter._heartbeat(self.ws, 0.005)
        self.ws.send.assert_awaited_once()
        self.ws.close.assert_awaited_once_with(code=4000, reason="heartbeat ACK timeout")

    def test_retry_and_decode_bounds(self):
        for attempt in range(10):
            self.assertGreaterEqual(self.adapter._retry_delay(attempt, None), 1)
            self.assertLessEqual(self.adapter._retry_delay(attempt, None), 60)
        self.assertGreaterEqual(self.adapter._retry_delay(0, 4008), 60)
        for frame in ("[]", "null", '{"op":true}', "bad-json"):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                self.adapter._decode(frame)

    async def test_failed_setup_revokes_partial_provider_registration(self):
        other = QQOfficialWebSocketAdapter("other-app", "secret")
        broken = SimpleNamespace(bot=Bot(), scheduler=SimpleNamespace(configure_adapter=Mock(side_effect=RuntimeError("failed"))))
        with self.assertRaisesRegex(RuntimeError, "failed"):
            await other.setup(broken)
        self.assertIsNone(other.runtime)
        self.assertEqual(broken.bot.providers.handles_for_owner("protocol.qqofficial"), ())
