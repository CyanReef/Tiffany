import asyncio
import importlib.util
import time
import unittest
from unittest.mock import AsyncMock

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("install the qqofficial extra to test its HTTP client")

from clients.QQOfficialClient import QQOfficialAPIError, QQOfficialClient
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
