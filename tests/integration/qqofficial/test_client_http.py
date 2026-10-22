import asyncio
import hashlib
import importlib.util
from pathlib import Path
import tempfile
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
        responses = [web.Response(status=502, text="upstream failed"), web.Response(status=302, headers={"Location": "/redirected"}), web.Response(status=204), web.json_response({"code": 304024, "message": "audit pending"}, status=202), web.Response(status=200)]
        async def respond(request, body):
            return responses.pop(0)
        self.peer.on_api = respond
        for status in (502, 302):
            with self.assertRaises(QQOfficialAPIError) as raised:
                await self.client.request("GET", "/response")
            self.assertEqual(raised.exception.status, status)
        self.assertIsNone(await self.client.request("GET", "/response"))
        self.assertEqual((await self.client.request("GET", "/response"))["code"], 304024)
        self.assertIsNone(await self.client.delete_message("private", "user", "sent-message"))
        self.assertEqual(len(self.peer.api_calls), 5)

    async def test_err_code_and_body_trace_are_preserved_and_auth_is_refreshed_once(self):
        async def respond(request, body):
            if request.headers["Authorization"] == "QQBot token-1":
                return web.json_response({"err_code": 11243, "message": "expired", "trace_id": "body-trace"})
            return web.json_response({"err_code": 40054013, "message": "user declined", "trace_id": "body-trace"})
        self.peer.on_api = respond
        with self.assertRaises(QQOfficialAPIError) as raised:
            await self.client.send_message("private", "user", json={"content": "hello", "msg_type": 0})
        self.assertEqual(raised.exception.code, 40054013)
        self.assertEqual(raised.exception.trace_id, "body-trace")
        self.assertEqual(self.peer.token_calls, 2)
        self.assertEqual(len(self.peer.api_calls), 2)

    async def test_local_file_upload_hashes_streams_confirms_and_merges_in_each_scene(self):
        data = bytes(range(256)) * 550 + b"tail"
        self.client.call_timeout = 3
        block_size = 65536
        count = (len(data) + block_size - 1) // block_size
        async def respond(request, body):
            if request.path.endswith("/upload_prepare"):
                self.assertEqual(body, {"file_type": 4, "file_size": str(len(data)), "file_name": "sample.bin",
                    "md5": hashlib.md5(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
                    "md5_10m": hashlib.md5(data).hexdigest()})
                return web.json_response({"upload_id": "task", "block_size": str(block_size), "parts": [
                    {"index": index, "presigned_url": self.peer.api_base + f"/upload/{index}"}
                    for index in range(count)
                ]})
            if request.path.endswith("/upload_part_finish"):
                index = body["part_index"]
                part = data[index * block_size:(index + 1) * block_size]
                self.assertEqual(body, {"upload_id": "task", "part_index": index,
                    "block_size": str(len(part)), "md5": hashlib.md5(part).hexdigest()})
                return web.Response(status=200)
            self.assertTrue(request.path.endswith("/files"))
            self.assertEqual(body, {"file_type": 4, "srv_send_msg": False, "upload_id": "task", "file_name": "sample.bin"})
            return web.json_response({"file_info": "opaque", "ttl": 300})
        self.peer.on_api = respond
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(data)
            for scene, segment in (("group", "groups"), ("private", "users")):
                with self.subTest(scene=scene):
                    self.peer.upload_calls.clear()
                    self.peer.api_calls.clear()
                    self.assertEqual(await self.client.upload_file(scene, "target", path), {"file_info": "opaque", "ttl": 300})
                    self.assertEqual(b"".join(call[1] for call in self.peer.upload_calls), data)
                    self.assertTrue(all(call[2] is None and call[3] == len(call[1]) for call in self.peer.upload_calls))
                    self.assertTrue(all(call[0].startswith(f"/v2/{segment}/target/") for call in self.peer.api_calls))
                    self.assertTrue(all(call[2] == "QQBot token-1" for call in self.peer.api_calls))
                    self.assertFalse(self.client._calls)

    async def test_failed_part_is_not_confirmed_merged_or_automatically_resent(self):
        async def respond(request, body):
            return web.json_response({"upload_id": "task", "block_size": "4", "parts": [
                {"index": 0, "presigned_url": self.peer.api_base + "/upload/0"},
            ]})
        async def fail(request, body):
            return web.Response(status=503)
        self.peer.on_api = respond
        self.peer.on_upload = fail
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(b"data")
            with self.assertRaises(QQOfficialAPIError) as raised:
                await self.client.upload_file("private", "u", path)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(len(self.peer.upload_calls), 1)
        self.assertEqual(len(self.peer.api_calls), 1)
        self.assertFalse(self.client._calls)

    async def test_abort_keeps_entire_upload_tracked_between_requests(self):
        from unittest.mock import AsyncMock
        entered = asyncio.Event()
        async def prepare(request, body):
            return web.json_response({"upload_id": "task", "block_size": "4", "parts": [
                {"index": 0, "presigned_url": self.peer.api_base + "/upload/0"},
            ]})
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.peer.on_api = prepare
        self.client.put_upload_part = AsyncMock(side_effect=blocked)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(b"data")
            task = asyncio.create_task(self.client.upload_file("group", "g", path))
            await entered.wait()
            self.assertIn(task, self.client._calls)
            await self.client.stop("abort")
            self.assertTrue(task.cancelled())
        self.assertFalse(self.client._calls)
        self.assertIsNone(self.client._session)

    async def test_invalid_upload_plan_and_oversized_put_response_release_resources(self):
        async def prepare(request, body):
            return web.json_response({"upload_id": "task", "block_size": "0", "parts": [
                {"index": 0, "presigned_url": self.peer.api_base + "/upload/0"},
            ]})
        self.peer.on_api = prepare
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.bin"
            path.write_bytes(b"data")
            with self.assertRaisesRegex(RuntimeError, "block_size"):
                await self.client.upload_file("group", "g", path)
            path.unlink()  # Windows also verifies the file handle was released.
        self.assertEqual(self.peer.upload_calls, [])
        async def oversized(request, body):
            return web.Response(body=b"x" * 65537)
        self.peer.on_upload = oversized
        with self.assertRaisesRegex(RuntimeError, "64 KiB"):
            await self.client.put_upload_part(self.peer.api_base + "/upload/0", b"data")
        self.assertFalse(self.client._calls)
