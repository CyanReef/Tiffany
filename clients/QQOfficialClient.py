"""QQ REST primitives and passive text replies; no message-model conversion."""

from __future__ import annotations

import asyncio
import json as jsonlib
import math
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

try:
    import aiohttp
except ModuleNotFoundError as error:
    if error.name != "aiohttp":
        raise
    raise ImportError('QQ official requires: python -m pip install -e ".[qqofficial]"') from None

from core import Context, Field
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, USER_ID


_REPLY_SEQ = Field[int]("qqofficial.reply_seq")
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"


class QQOfficialClientError(RuntimeError):
    """Lifecycle, transport or response-format failure."""


class QQOfficialTransportError(QQOfficialClientError):
    """Network failure with an unknown send outcome."""


class QQOfficialAPIError(QQOfficialClientError):
    def __init__(self, status: int, code: int | None, message: str, trace_id: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        super().__init__(f"QQ API HTTP {status}, code={code}: {message} (trace={trace_id})")


class QQOfficialClient:
    def __init__(
        self, app_id: str, app_secret: str, *, sandbox: bool = False,
        call_timeout: float = 30.0, max_inflight: int = 16,
        api_base: str | None = None, token_url: str = TOKEN_URL,
    ):
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError("QQ app_id must be a non-empty string")
        if not isinstance(app_secret, str) or not app_secret:
            raise ValueError("QQ app_secret must be a non-empty string")
        if (not math.isfinite(call_timeout) or call_timeout <= 0
                or type(max_inflight) is not int or max_inflight < 1):
            raise ValueError("call_timeout must be finite and positive; max_inflight at least 1")
        self.app_id = app_id
        self._app_secret = app_secret
        self.call_timeout = call_timeout
        self.api_base = (api_base or (SANDBOX_API_BASE if sandbox else API_BASE)).rstrip("/")
        self.token_url = token_url
        self.max_inflight = max_inflight
        self.runtime: object | None = None
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_inflight)
        self._calls: set[asyncio.Task[Any]] = set()
        self._closing = False

    async def setup(self, runtime: object) -> None:
        if self.runtime is not None and self.runtime is not runtime:
            raise QQOfficialClientError("client already belongs to another runtime")
        self.runtime = runtime

    async def start(self) -> None:
        if self._session is not None:
            return
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=self.max_inflight),
            timeout=aiohttp.ClientTimeout(total=self.call_timeout),
        )
        self._closing = False

    async def stop(self, mode: str = "drain") -> None:
        if mode not in ("drain", "abort"):
            raise ValueError("stop mode must be 'drain' or 'abort'")
        self._closing = True
        calls = tuple(task for task in self._calls if task is not asyncio.current_task())
        if mode == "abort":
            for task in calls:
                task.cancel()
        try:
            if calls:
                await asyncio.gather(*(asyncio.shield(task) for task in calls), return_exceptions=True)
        except BaseException:
            # If a drain deadline expires, also settle callers waiting for slots
            # or the token lock before releasing their shared HTTP session.
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            raise
        finally:
            session, self._session = self._session, None
            if session is not None:
                await session.close()
            self.invalidate_access_token()

    async def teardown(self) -> None:
        await self.stop("abort")

    def invalidate_access_token(self, used_token: str | None = None) -> None:
        # A late 401 must not invalidate a replacement fetched by another call.
        if used_token is None or self._token == used_token:
            self._token = None
            self._refresh_at = 0.0

    @asynccontextmanager
    async def _call(self):
        if self._closing or self._session is None or self._session.closed:
            raise QQOfficialClientError("QQ client is not running")
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            async with asyncio.timeout(self.call_timeout):
                yield
        except TimeoutError:
            raise TimeoutError("QQ API call timed out; a send may already have succeeded") from None
        except aiohttp.ClientError:
            raise QQOfficialTransportError("QQ API connection failed; send result may be unknown") from None
        finally:
            self._calls.discard(task)

    async def get_access_token(self) -> str:
        async with self._call():
            return await self._get_access_token()

    async def _get_access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._refresh_at:
            return self._token
        async with self._token_lock:
            if self._token is not None and time.monotonic() < self._refresh_at:
                return self._token
            async with asyncio.timeout(min(10.0, self.call_timeout)):
                data = await self._http(
                    "POST", self.token_url,
                    json={"appId": self.app_id, "clientSecret": self._app_secret},
                )
            if (not isinstance(data, dict)
                    or not isinstance(data.get("access_token"), str)
                    or not data["access_token"]):
                raise QQOfficialClientError("QQ token response has no access_token")
            try:
                lifetime = float(data["expires_in"])
            except (KeyError, TypeError, ValueError):
                raise QQOfficialClientError("QQ token response has invalid expires_in") from None
            if not math.isfinite(lifetime) or lifetime <= 0:
                raise QQOfficialClientError("QQ token response has invalid expires_in")
            self._token = data["access_token"]
            self._refresh_at = time.monotonic() + lifetime - min(60.0, lifetime / 2)
            return self._token

    async def request(self, method: str, path: str, *, json: Any = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("QQ API path must be relative to its configured API base")
        async with self._call():
            for attempt in range(2):
                token = await self._get_access_token()
                try:
                    return await self._http(method, self.api_base + path, json=json, token=token)
                except QQOfficialAPIError as error:
                    if attempt or (error.status != 401 and error.code != 11243):
                        raise
                    self.invalidate_access_token(token)
        raise AssertionError("unreachable")

    def _safe_text(self, value: Any, used_token: str | None = None) -> str:
        text = str(value)
        for credential in (self._app_secret, self._token, used_token):
            if credential:
                text = text.replace(credential, "<redacted>")
        return text.replace("\n", " ")[:256]

    async def _http(self, method: str, url: str, *, json: Any, token: str | None = None) -> Any:
        headers = {"User-Agent": "Tiffany/0.1", "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"QQBot {token}"
        async with self._slots:
            async with self._session.request(
                method, url, json=json, headers=headers, allow_redirects=False,
            ) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise QQOfficialClientError("QQ API response exceeds 1 MiB")
                trace_id = self._safe_text(response.headers.get("X-Tps-Trace-ID", ""), token)
                if response.status == 204:
                    return None
                try:
                    data = jsonlib.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise QQOfficialAPIError(response.status, None, "response is not JSON", trace_id) from None
                code = data.get("code", data.get("err_code")) if isinstance(data, dict) else None
                accepted_audit = response.status in (201, 202) and code in (304023, 304024)
                if not 200 <= response.status < 300 or (code not in (None, 0) and not accepted_audit):
                    message = data.get("message", "request failed") if isinstance(data, dict) else "request failed"
                    raise QQOfficialAPIError(response.status, code, self._safe_text(message, token), trace_id)
                return data

    async def reply(self, ctx: Context, text: str) -> Any:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("reply text must be non-empty")
        scene = ctx.resolve(MESSAGE_TYPE)
        target = ctx.resolve(GROUP_ID) if scene == "group" else ctx.resolve(USER_ID)
        message_id = ctx.resolve(MESSAGE_ID)
        if (scene not in ("group", "private")
                or not isinstance(target, str) or not target
                or not isinstance(message_id, str) or not message_id):
            raise ValueError("QQ passive reply requires a supported scene, OpenID and message ID")
        sequence = (ctx.resolve(_REPLY_SEQ) if ctx.has(_REPLY_SEQ) else 0) + 1
        ctx.put(_REPLY_SEQ, sequence)
        segment = "groups" if scene == "group" else "users"
        return await self.request(
            "POST", f"/v2/{segment}/{quote(target, safe='')}/messages",
            json={"content": text, "msg_type": 0, "msg_id": message_id, "msg_seq": sequence},
        )

    async def reply_text(self, ctx: Context, text: str) -> Any:
        return await self.reply(ctx, text)
