import asyncio
import importlib.util
import time
import unittest

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("install the qqofficial extra for local HTTP tests")

from aiohttp import web
from clients.QQOfficialClient import QQOfficialAPIError, QQOfficialClient
from tests.support.qqofficial import QQTestServer, eventually


class QQOfficialHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = await QQTestServer().start()
        self.client = QQOfficialClient("app", "test-secret", api_base=self.peer.api_base, token_url=self.peer.token_url, call_timeout=0.5, max_inflight=2)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.teardown()
        if self.peer.token_gate is not None:
            self.peer.token_gate.set()
        await self.peer.close()

    async def test_real_http_singleflight_headers_and_401_refresh_once(self):
        self.assertEqual(await asyncio.gather(*(self.client.get_access_token() for _ in range(8))), ["token-1"] * 8)
        self.assertEqual(self.peer.token_calls, 1)
        async def authenticated(request, body):
            if request.headers["Authorization"] == "QQBot token-1":
                return web.json_response({"code": 11243, "message": "expired"}, status=401)
            return web.json_response({"id": "sent"})
        self.peer.on_api = authenticated
        body = {"msg_id": "source", "msg_seq": 1, "msg_type": 0, "content": "hello"}
        self.assertEqual(await self.client.request("POST", "/v2/users/u/messages", json=body), {"id": "sent"})
        self.assertEqual(self.peer.token_calls, 2)
        self.assertEqual([call[2] for call in self.peer.api_calls], ["QQBot token-1", "QQBot token-2"])
        self.assertEqual(self.peer.api_calls[0][1], self.peer.api_calls[1][1])

    async def test_repeated_401_and_business_failure_stop_with_trace_and_redacted_message(self):
        async def fail(request, body):
            return web.json_response({"code": 11243, "message": "test-secret " + request.headers["Authorization"]}, status=401, headers={"X-Tps-Trace-ID": "trace-123"})
        self.peer.on_api = fail
        with self.assertRaises(QQOfficialAPIError) as raised:
            await self.client.request("POST", "/v2/users/u/messages", json={})
        self.assertEqual(len(self.peer.api_calls), 2)
        self.assertEqual(raised.exception.trace_id, "trace-123")
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertNotIn("token-", str(raised.exception))
        async def limited(request, body):
            return web.json_response({"code": 22009, "message": "limited"})
        self.peer.on_api = limited
        with self.assertRaises(QQOfficialAPIError) as limited_error:
            await self.client.request("POST", "/v2/users/u/messages", json={})
        self.assertEqual(limited_error.exception.code, 22009)
        self.assertEqual(len(self.peer.api_calls), 3)

    async def test_concurrency_bound_timeout_includes_waiting_and_no_resend(self):
        active = peak = 0
        release = asyncio.Event()
        async def blocked(request, body):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
                return web.json_response({"id": "sent"})
            finally:
                active -= 1
        self.peer.on_api = blocked
        self.client.call_timeout = 0.08
        tasks = [asyncio.create_task(self.client.request("POST", "/send", json={})) for _ in range(6)]
        started = time.monotonic()
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue(all(isinstance(result, TimeoutError) for result in results))
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertEqual(peak, 2)
            self.assertEqual(len(self.peer.api_calls), 2)
            self.assertFalse(self.client._calls)
        finally:
            release.set()

    async def test_cancelled_refresh_does_not_poison_following_token_request(self):
        self.peer.token_gate = asyncio.Event()
        first = asyncio.create_task(self.client.get_access_token())
        await eventually(lambda: self.peer.token_calls == 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.peer.token_gate.set()
        self.assertEqual(await self.client.get_access_token(), "token-2")
        self.assertFalse(self.client._token_lock.locked())

    async def test_non_json_redirect_empty_and_audit_responses(self):
        responses = [web.Response(status=502, text="upstream failed"), web.Response(status=302, headers={"Location": "/redirected"}), web.Response(status=204), web.json_response({"code": 304024, "message": "audit pending"}, status=202)]
        async def respond(request, body):
            return responses.pop(0)
        self.peer.on_api = respond
        for status in (502, 302):
            with self.assertRaises(QQOfficialAPIError) as raised:
                await self.client.request("GET", "/response")
            self.assertEqual(raised.exception.status, status)
        self.assertIsNone(await self.client.request("GET", "/response"))
        self.assertEqual((await self.client.request("GET", "/response"))["code"], 304024)
        self.assertEqual(len(self.peer.api_calls), 4)
