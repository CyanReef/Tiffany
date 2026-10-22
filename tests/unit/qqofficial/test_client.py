import asyncio
import importlib.util
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("install the qqofficial extra to test its HTTP client")

from clients.QQOfficialClient import QQOfficialAPIError, QQOfficialClient, _file_hashes
from core import Bot, Context, Envelope
from adapters.qqofficial_fields import register_qqofficial_fields


class QQOfficialClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = QQOfficialClient("app", "secret", call_timeout=0.2)
        await self.client.start()
        self.client._http = AsyncMock(return_value={"access_token": "token", "expires_in": "7200"})

    async def asyncTearDown(self):
        await self.client.teardown()

    def context(self, scene="group"):
        bot = Bot()
        register_qqofficial_fields(bot, "qq_official", lambda: "bot")
        raw = {"t": "GROUP_AT_MESSAGE_CREATE" if scene == "group" else "C2C_MESSAGE_CREATE", "d": {
            "id": "source-message", "group_openid": "group/id", "author": {"user_openid": "user/id"},
        }}
        return Context(Envelope("qq_official", raw, client=self.client), bot.providers)

    async def test_concurrent_token_requests_share_one_fetch_and_refresh_near_expiry(self):
        async def fetch(*args, **kwargs):
            await asyncio.sleep(0.01)
            return {"access_token": "token", "expires_in": "7200"}
        self.client._http.side_effect = fetch
        tokens = await asyncio.gather(*(self.client.get_access_token() for _ in range(12)))
        self.assertEqual(tokens, ["token"] * 12)
        self.assertEqual(self.client._http.await_count, 1)
        self.client._refresh_at = time.monotonic() - 1
        await self.client.get_access_token()
        self.assertEqual(self.client._http.await_count, 2)

    async def test_auth_retry_reuses_reply_body_and_late_401_keeps_new_token(self):
        await self.client.get_access_token()
        self.client._http.reset_mock()
        self.client._http.side_effect = [
            QQOfficialAPIError(401, 11243, "invalid token"),
            {"access_token": "new-token", "expires_in": "7200"}, {"id": "sent"},
        ]
        self.assertEqual(await self.client.reply(self.context(), "hello"), {"id": "sent"})
        calls = self.client._http.await_args_list
        self.assertEqual(calls[0].kwargs["json"], calls[2].kwargs["json"])
        self.assertEqual(calls[2].kwargs["json"]["msg_seq"], 1)
        self.client.invalidate_access_token("token")
        self.assertEqual(self.client._token, "new-token")

    async def test_concurrent_replies_have_distinct_sequences_and_encoded_targets(self):
        await self.client.get_access_token()
        self.client._http.reset_mock()
        self.client._http.return_value = {"id": "sent"}
        ctx = self.context()
        await asyncio.gather(self.client.reply(ctx, "one"), self.client.reply_text(ctx, "two"))
        calls = self.client._http.await_args_list
        self.assertEqual([call.kwargs["json"]["msg_seq"] for call in calls], [1, 2])
        self.assertTrue(all(call.args[1].endswith("/v2/groups/group%2Fid/messages") for call in calls))
        await self.client.reply(self.context("private"), "private")
        self.assertTrue(self.client._http.await_args.args[1].endswith("/v2/users/user%2Fid/messages"))
        self.assertEqual(self.client._http.await_args.kwargs["json"]["msg_seq"], 1)

    def raw_context(self, raw):
        bot = Bot()
        register_qqofficial_fields(bot, "qq_official", lambda: "bot")
        return Context(Envelope("qq_official", raw, client=self.client), bot.providers)

    async def test_rich_replies_share_sequence_without_mutating_the_callers_body(self):
        await self.client.get_access_token()
        self.client._http.reset_mock()
        self.client._http.return_value = {"id": "sent"}
        ctx = self.context()
        markdown = {"msg_type": 2, "markdown": {"content": "# hello"}, "keyboard": {"id": "keyboard"}}
        media = {"msg_type": 7, "media": {"file_info": "opaque"}}
        await self.client.reply_message(ctx, json=markdown)
        await self.client.reply_message(ctx, json=media)
        await ctx.reply("text")
        bodies = [call.kwargs["json"] for call in self.client._http.await_args_list]
        self.assertEqual([body["msg_seq"] for body in bodies], [1, 2, 3])
        self.assertTrue(all(body["msg_id"] == "source-message" for body in bodies))
        self.assertNotIn("content", bodies[0])
        self.assertNotIn("msg_seq", markdown)
        self.assertNotIn("msg_id", media)

    async def test_channel_and_direct_replies_use_distinct_endpoints_and_native_bodies(self):
        self.client.request = AsyncMock(return_value={"id": "sent"})
        for event, scene, target, path in (
            ("AT_MESSAGE_CREATE", "channel", {"channel_id": "c/id", "guild_id": "g/id"}, "/channels/c%2Fid/messages"),
            ("DIRECT_MESSAGE_CREATE", "direct", {"channel_id": "c/id", "guild_id": "g/id"}, "/dms/g%2Fid/messages"),
        ):
            with self.subTest(scene=scene):
                ctx = self.raw_context({"t": event, "d": target | {"id": "source", "author": {"id": "user"}}})
                await ctx.reply("hello")
                self.client.request.assert_awaited_with("POST", path, json={"content": "hello", "msg_id": "source"})

    async def test_event_reply_uses_outer_id_but_acknowledgement_uses_inner_id(self):
        self.client.request = AsyncMock(return_value={})
        ctx = self.raw_context({"t": "INTERACTION_CREATE", "id": "INTERACTION_CREATE:inner", "d": {
            "id": "inner", "scene": "group", "group_openid": "g", "group_member_openid": "u",
        }})
        await ctx.reply("accepted")
        self.client.request.assert_awaited_with("POST", "/v2/groups/g/messages", json={
            "content": "accepted", "msg_type": 0, "event_id": "INTERACTION_CREATE:inner", "msg_seq": 1,
        })
        await self.client.acknowledge_interaction(ctx.raw["d"]["id"])
        self.client.request.assert_awaited_with("PUT", "/interactions/inner", json={"code": 0})
        for event, data, path in (("FRIEND_ADD", {"openid": "u"}, "/v2/users/u/messages"),
                                  ("GROUP_ADD_ROBOT", {"group_openid": "g"}, "/v2/groups/g/messages")):
            await ctx.client.reply(self.raw_context({"t": event, "id": "outer", "d": data}), "welcome")
            self.assertEqual(self.client.request.await_args.args[1], path)
            self.assertEqual(self.client.request.await_args.kwargs["json"]["event_id"], "outer")

    async def test_passive_reply_rejects_unsupported_events_and_overridden_references(self):
        self.client.request = AsyncMock()
        ctx = self.context()
        for field in ("msg_id", "event_id", "msg_seq", "is_wakeup"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                await self.client.reply_message(ctx, json={"content": "hello", field: "override"})
        with self.assertRaisesRegex(ValueError, "does not support"):
            await self.client.reply(self.raw_context({"t": "FRIEND_DEL", "d": {"openid": "u"}}), "bye")
        self.client.request.assert_not_awaited()

    async def test_active_stream_typing_and_delete_primitives_keep_native_payloads(self):
        self.client.request = AsyncMock(return_value={"id": "stream"})
        await self.client.send_message("private", "u/id", json={"msg_type": 6, "input_notify": {"input_type": 1, "input_second": 60}})
        self.assertEqual(self.client.request.await_args.args[1], "/v2/users/u%2Fid/messages")
        for index in range(3):
            body = {"msg_id": "source", "msg_seq": 7, "index": index,
                    "input_state": 10 if index == 2 else 1, "content_raw": "piece", "content_type": "markdown"}
            if index:
                body["stream_msg_id"] = "stream"
            await self.client.send_stream_message("u/id", json=body)
            self.client.request.assert_awaited_with("POST", "/v2/users/u%2Fid/stream_messages", json=body)
        for scene, path in (("group", "/v2/groups/t/messages/m%2Fid"),
                            ("private", "/v2/users/t/messages/m%2Fid"),
                            ("channel", "/channels/t/messages/m%2Fid?hidetip=true"),
                            ("direct", "/dms/t/messages/m%2Fid?hidetip=true")):
            await self.client.delete_message(scene, "t", "m/id", hidetip=True)
            self.client.request.assert_awaited_with("DELETE", path)

    async def test_url_upload_and_part_merge_never_send_implicitly(self):
        self.client.request = AsyncMock(return_value={"file_info": "opaque", "ttl": 300})
        await self.client.upload_media("group", "g/id", file_type=1, url="https://example.com/image.png")
        self.client.request.assert_awaited_with("POST", "/v2/groups/g%2Fid/files", json={
            "file_type": 1, "srv_send_msg": False, "url": "https://example.com/image.png",
        })
        await self.client.upload_media("private", "u", file_type=4, upload_id="upload", file_name="file.txt")
        self.client.request.assert_awaited_with("POST", "/v2/users/u/files", json={
            "file_type": 4, "srv_send_msg": False, "upload_id": "upload", "file_name": "file.txt",
        })
        self.client.request.reset_mock()
        for options in ({}, {"url": "file:///tmp/image"}, {"url": "http://example.com", "upload_id": "upload"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                await self.client.upload_media("group", "g", file_type=1, **options)
        for scene in ("channel", "direct"):
            with self.subTest(scene=scene), self.assertRaises(ValueError):
                await self.client.upload_media(scene, "target", file_type=1, url="https://example.com")
        self.client.request.assert_not_awaited()

    async def test_message_reference_and_wakeup_conflicts_are_rejected(self):
        self.client.request = AsyncMock()
        for body in ({"msg_id": "m", "event_id": "e"}, {"msg_id": "m", "is_wakeup": True}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                await self.client.send_message("private", "u", json=body)
        for code in (True, -1, 6):
            with self.subTest(code=code), self.assertRaises(ValueError):
                await self.client.acknowledge_interaction("interaction", code)
        self.client.request.assert_not_awaited()

    def test_file_prefix_hash_uses_the_official_byte_count(self):
        data = b"a" * 10002432 + b"different suffix"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(data)
            info = _file_hashes(path)
        self.assertEqual(info["md5_10m"], hashlib.md5(data[:10002432]).hexdigest())
        self.assertEqual(info["md5"], hashlib.md5(data).hexdigest())
        self.assertEqual(info["sha1"], hashlib.sha1(data).hexdigest())
        self.assertEqual(info["file_size"], str(len(data)))

    async def test_invalid_local_files_fail_before_network_io(self):
        self.client.request = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.bin"
            path.touch()
            for invalid in (path, Path(directory), path.with_name("missing.bin")):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    await self.client.upload_file("group", "g", invalid)
        self.client.request.assert_not_awaited()

    async def test_timeout_and_cancellation_do_not_retry_unknown_send(self):
        await self.client.get_access_token()
        self.client._http.reset_mock()
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        self.client._http.side_effect = blocked
        with self.assertRaisesRegex(TimeoutError, "may already have succeeded"):
            await self.client.request("POST", "/send", json={})
        self.assertEqual(self.client._http.await_count, 1)
        task = asyncio.create_task(self.client.request("POST", "/send"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._calls, set())

    async def test_permanent_failure_has_no_retry_and_failed_refresh_releases_lock(self):
        self.client._http.side_effect = QQOfficialAPIError(429, 22009, "rate limited", "trace")
        with self.assertRaises(QQOfficialAPIError) as raised:
            await self.client.request("POST", "/send")
        self.assertEqual(raised.exception.trace_id, "trace")
        self.assertEqual(self.client._http.await_count, 1)
        self.assertFalse(self.client._token_lock.locked())

    async def test_invalid_ttl_and_absolute_api_url_are_rejected(self):
        self.client._http.return_value = {"access_token": "token", "expires_in": "nan"}
        with self.assertRaisesRegex(RuntimeError, "expires_in"):
            await self.client.get_access_token()
        with self.assertRaises(ValueError):
            await self.client.request("GET", "https://another.example/api")

    async def test_stop_abort_settles_calls_and_is_idempotent(self):
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.client._http.side_effect = blocked
        task = asyncio.create_task(self.client.get_access_token())
        await started.wait()
        await self.client.stop("abort")
        self.assertTrue(task.cancelled())
        self.assertEqual(self.client._calls, set())
        self.assertIsNone(self.client._session)
        await self.client.teardown()

    async def test_interrupted_drain_settles_token_lock_waiters_before_closing(self):
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.client._http.side_effect = blocked
        calls = [asyncio.create_task(self.client.get_access_token()) for _ in range(3)]
        await started.wait()
        stopping = asyncio.create_task(self.client.stop("drain"))
        await asyncio.sleep(0)
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertTrue(all(task.cancelled() for task in calls))
        self.assertFalse(self.client._calls)
        self.assertFalse(self.client._token_lock.locked())
        self.assertIsNone(self.client._session)
