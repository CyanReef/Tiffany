"""Real Runtime + local HTTP/WebSocket peers, including failure recovery."""

import asyncio
import importlib.util
import unittest

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("install the qqofficial extra for local gateway tests")

from adapters.QQOfficialWebSocketAdapter import QQOfficialGatewayError, QQOfficialWebSocketAdapter
from clients.QQOfficialClient import QQOfficialClient, QQOfficialClientError
from core import Bot, RuntimeState
from fields import MESSAGE_ID, SELF_ID, TEXT
from hooks import register_hooks
from tests.support.qqofficial import QQTestServer, eventually, message


class QQOfficialGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = await QQTestServer().start()
        self.bot = Bot()
        self.client = QQOfficialClient("app", "test-secret", api_base=self.peer.api_base, token_url=self.peer.token_url)
        self.adapter = QQOfficialWebSocketAdapter("app", "test-secret", client=self.client)
        self.adapter._retry_delay = lambda attempt, code: 0.01
        self.bot.install(self.adapter)

    async def asyncTearDown(self):
        await self.bot.stop("abort")
        await self.peer.close()

    async def connection(self):
        async with asyncio.timeout(2):
            return await self.peer.connections.get()

    async def test_group_and_private_ping_reuse_hook_with_unmodified_raw_and_dedup(self):
        register_hooks(self.bot)
        captured = []
        @self.bot.hook(on="message", priority=10)
        async def inspect(ctx):
            self.assertFalse(ctx.has(TEXT))
            self.assertEqual(ctx.resolve(SELF_ID), "bot-user")
            captured.append(ctx.raw)
        await self.bot.start()
        ws = await self.connection()
        group = message(seq=2, message_id="same-id", target="same-openid", content="ping")
        private = message("private", seq=3, message_id="same-id", target="same-openid", content=" PiNg ")
        await ws.send_json(group)
        await ws.send_json(private)
        await ws.send_json(message(seq=4, message_id="same-id", target="same-openid", content="ping"))
        await eventually(lambda: self.adapter._admitted_seq == 4 and self.bot.runtime.scheduler.idle)
        self.assertEqual(captured, [group, private])
        self.assertEqual([call[0] for call in self.peer.api_calls], ["/v2/groups/same-openid/messages", "/v2/users/same-openid/messages"])
        self.assertTrue(all(call[1] == {"content": "pong", "msg_type": 0, "msg_id": "same-id", "msg_seq": 1} for call in self.peer.api_calls))
        self.assertTrue(all(call[2] == "QQBot token-1" for call in self.peer.api_calls))
        self.assertEqual(self.peer.auth_payloads[0]["d"]["intents"], 1 << 25)
        self.assertEqual(self.peer.auth_payloads[0]["d"]["shard"], [0, 1])

    async def test_disconnect_resumes_from_admitted_sequence_and_ignores_replayed_message(self):
        seen = []
        @self.bot.hook(on="message")
        async def record(ctx):
            seen.append(ctx.resolve(MESSAGE_ID))
        await self.bot.start()
        first = await self.connection()
        await first.send_json(message(seq=2))
        await eventually(lambda: seen == ["message-2"])
        await first.close(code=4000)
        second = await self.connection()
        self.assertEqual(self.peer.auth_payloads[1], {"op": 6, "d": {"token": "QQBot token-1", "session_id": "gateway-session", "seq": 2}})
        await second.send_json(message(seq=3, message_id="message-2"))
        await second.send_json(message(seq=4))
        await eventually(lambda: self.adapter._admitted_seq == 4 and self.bot.runtime.scheduler.idle)
        self.assertEqual(seen, ["message-2", "message-4"])
        self.assertEqual(self.peer.token_calls, 1)
        self.assertFalse(self.bot.runtime.tasks.failures)

    async def test_invalid_session_clears_resume_and_identifies_again(self):
        async def auth(ws, payload, index):
            if index == 2:
                await ws.send_json({"op": 9, "d": False})
            else:
                await self.peer.ready(ws)
        self.peer.on_auth = auth
        await self.bot.start()
        first = await self.connection()
        await eventually(lambda: self.adapter._session_id is not None)
        await first.close(code=4000)
        await eventually(lambda: len(self.peer.auth_payloads) >= 3)
        self.assertEqual([payload["op"] for payload in self.peer.auth_payloads[:3]], [2, 6, 2])
        await eventually(lambda: self.adapter.self_id == "bot-user" and self.adapter._admitted_seq == 1)

    async def test_lost_heartbeat_ack_reconnects_without_abandoned_tasks(self):
        self.peer.ack_heartbeat = False
        async def auth(ws, payload, index):
            if index > 1:
                self.peer.ack_heartbeat = True
                await ws.send_json({"op": 0, "s": payload["d"]["seq"], "t": "RESUMED", "d": ""})
            else:
                await self.peer.ready(ws)
        self.peer.on_auth = auth
        await self.bot.start()
        await eventually(lambda: len(self.peer.auth_payloads) >= 2)
        self.assertEqual(self.peer.auth_payloads[1]["op"], 6)
        report = await self.bot.stop("drain")
        self.assertTrue(report.successful)
        self.assertFalse(self.bot.runtime.tasks.failures)
        self.assertEqual(self.bot.runtime.tasks.active_count, 0)

    async def test_overload_recovery_replays_only_unadmitted_message(self):
        self.bot.runtime.scheduler.capacity = 1
        gate = asyncio.Event()
        entered = asyncio.Event()
        seen = []
        @self.bot.hook(on="message")
        async def slow(ctx):
            seen.append(ctx.resolve(MESSAGE_ID))
            entered.set()
            await gate.wait()
        async def auth(ws, payload, index):
            if index == 1:
                await self.peer.ready(ws)
            else:
                await gate.wait()
                await eventually(lambda: self.bot.runtime.scheduler.idle)
                await ws.send_json(message(seq=3))
                await ws.send_json({"op": 0, "s": 3, "t": "RESUMED", "d": ""})
        self.peer.on_auth = auth
        try:
            await self.bot.start()
            first = await self.connection()
            await first.send_json(message(seq=2))
            await entered.wait()
            await first.send_json(message(seq=3))
            await eventually(lambda: len(self.peer.auth_payloads) >= 2)
            self.assertEqual(self.peer.auth_payloads[1]["d"]["seq"], 2)
            self.assertNotIn(("group", "openid", "message-3"), self.adapter._seen)
            gate.set()
            await eventually(lambda: seen == ["message-2", "message-3"])
        finally:
            gate.set()

    async def test_slow_session_keeps_heartbeats_and_other_sessions_running(self):
        gate = asyncio.Event()
        first_entered = asyncio.Event()
        other_entered = asyncio.Event()
        seen = []
        @self.bot.hook(on="message")
        async def work(ctx):
            mid = ctx.resolve(MESSAGE_ID)
            seen.append(mid)
            if mid == "message-2":
                first_entered.set()
                await gate.wait()
            if mid == "message-4":
                other_entered.set()
        try:
            await self.bot.start()
            ws = await self.connection()
            await ws.send_json(message(seq=2, target="group-a"))
            await first_entered.wait()
            await ws.send_json(message(seq=3, target="group-a"))
            await ws.send_json(message(seq=4, target="group-b"))
            async with asyncio.timeout(2):
                await other_entered.wait()
                await self.peer.heartbeat_received.wait()
            self.assertEqual(seen, ["message-2", "message-4"])
            self.assertEqual(len(self.bot.runtime.tasks.tasks_for(self.adapter)), 2)
            gate.set()
            await eventually(lambda: self.bot.runtime.scheduler.idle)
            self.assertEqual(seen, ["message-2", "message-4", "message-3"])
        finally:
            gate.set()

    async def test_drain_keeps_client_alive_until_reply_finishes(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        @self.bot.hook(on="message")
        async def delayed_reply(ctx):
            entered.set()
            await gate.wait()
            await ctx.reply("finished during drain")
        try:
            await self.bot.start()
            ws = await self.connection()
            await ws.send_json(message())
            await entered.wait()
            closing = asyncio.create_task(self.bot.stop("drain"))
            await eventually(lambda: self.bot.runtime.state == RuntimeState.STOPPING_DRAIN)
            self.assertIsNotNone(self.client._session)
            gate.set()
            report = await closing
            self.assertTrue(report.successful)
            self.assertEqual(self.peer.api_calls[0][1]["content"], "finished during drain")
            self.assertIsNone(self.client._session)
            self.assertFalse(self.adapter._providers)
        finally:
            gate.set()

    async def test_abort_cancels_inflight_reply_and_releases_resources(self):
        gate = asyncio.Event()
        async def blocked(request, body):
            self.peer.reply_received.set()
            await gate.wait()
            from aiohttp import web
            return web.json_response({"id": "sent"})
        self.peer.on_api = blocked
        register_hooks(self.bot)
        try:
            await self.bot.start()
            ws = await self.connection()
            await ws.send_json(message(content="ping"))
            async with asyncio.timeout(2):
                await self.peer.reply_received.wait()
            report = await self.bot.stop("abort")
            self.assertTrue(report.successful)
            self.assertEqual(self.bot.runtime.tasks.active_count, 0)
            self.assertFalse(self.client._calls)
            self.assertIsNone(self.client._session)
            self.assertIsNone(self.adapter._ws)
        finally:
            gate.set()

    async def test_failed_start_rolls_back_http_session_and_providers(self):
        self.peer.token_response = {"access_token": "token", "expires_in": "invalid"}
        with self.assertRaises(QQOfficialClientError):
            await self.bot.start()
        self.assertEqual(self.bot.runtime.state, RuntimeState.STOP_FAILED)
        self.assertIsNone(self.client._session)
        self.assertEqual(self.bot.providers.handles_for_owner("protocol.qqofficial"), ())
        self.assertEqual(self.bot.runtime.tasks.active_count, 0)

    async def test_auth_failure_refreshes_once_then_supervisor_stops_runtime(self):
        async def rejected(ws, payload, index):
            await ws.close(code=4004)
        self.peer.on_auth = rejected
        with self.assertLogs("core.TaskRegistry", level="ERROR"):
            await self.bot.start()
            await eventually(lambda: self.bot.runtime.state == RuntimeState.TERMINATED)
        self.assertEqual(self.peer.token_calls, 2)
        failures = self.bot.runtime.tasks.failures
        self.assertTrue(any(isinstance(error, QQOfficialGatewayError) for info, error in failures))
        self.assertIsNone(self.client._session)

    async def test_malformed_frames_notice_and_unknown_events_do_not_break_connection(self):
        received = []
        @self.bot.hook()
        async def record(ctx):
            received.append((ctx.kind, ctx.raw))
        await self.bot.start()
        ws = await self.connection()
        with self.assertLogs("adapters.QQOfficialWebSocketAdapter", level="WARNING"):
            await ws.send_str("bad-json")
            await ws.send_str("[]")
            await ws.send_json({"op": 0, "s": 2, "t": "FRIEND_ADD", "d": {"extra": "preserved"}})
            await ws.send_json({"op": 0, "s": 3, "t": "FUTURE_EVENT", "d": {"future": True}})
            await eventually(lambda: len(received) == 2)
        self.assertEqual([kind for kind, raw in received], ["notice", "event"])
        self.assertEqual(received[1][1]["d"], {"future": True})
        self.assertEqual(self.bot.runtime.state, RuntimeState.RUNNING)
