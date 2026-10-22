"""QQ REST and message primitives; payloads and responses stay native JSON."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import hashlib
import json as jsonlib
import math
from pathlib import Path
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
from adapters.qqofficial_fields import CHANNEL_ID, GUILD_ID, EVENT_ID, MESSAGE_EVENTS, REPLY_EVENTS


_REPLY_SEQ = Field[int]("qqofficial.reply_seq")
API_BASE = "https://api.bot.qq.com"
TOKEN_URL = API_BASE + "/app/getAppAccessToken"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
_SCENE_PATHS = {"group": "/v2/groups", "private": "/v2/users", "channel": "/channels", "direct": "/dms"}
_REPLY_FIELDS = {"group": GROUP_ID, "private": USER_ID, "channel": CHANNEL_ID, "direct": GUILD_ID}
_MAX_FILE_SIZE = 200 * 1024 * 1024


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("QQ target and message IDs must be non-empty strings")
    return quote(value, safe="")


def _scene_path(scene: str, target: str) -> str:
    if scene not in _SCENE_PATHS:
        raise ValueError("QQ scene must be group, private, channel or direct")
    return f"{_SCENE_PATHS[scene]}/{_identifier(target)}"


def _file_hashes(path: Path) -> dict[str, str]:
    md5, sha1, first_md5 = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            first_md5.update(chunk[:max(0, 10002432 - size)])
            md5.update(chunk)
            sha1.update(chunk)
            size += len(chunk)
            if size > _MAX_FILE_SIZE:
                raise ValueError("QQ files must not exceed 200 MiB")
    if not size:
        raise ValueError("QQ files must be non-empty")
    return {"file_size": str(size), "file_name": path.name,
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": first_md5.hexdigest()}


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
        nested = task in self._calls
        self._calls.add(task)
        try:
            async with asyncio.timeout(self.call_timeout):
                yield
        except TimeoutError:
            raise TimeoutError("QQ API call timed out; a send may already have succeeded") from None
        except aiohttp.ClientError:
            raise QQOfficialTransportError("QQ API connection failed; send result may be unknown") from None
        finally:
            if not nested:
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
                if response.status == 204 or (not body and 200 <= response.status < 300):
                    return None
                try:
                    data = jsonlib.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise QQOfficialAPIError(response.status, None, "response is not JSON", trace_id) from None
                code = data.get("err_code", data.get("code")) if isinstance(data, dict) else None
                if not trace_id and isinstance(data, dict):
                    trace_id = self._safe_text(data.get("trace_id", ""), token)
                accepted_audit = response.status in (201, 202) and code in (304023, 304024)
                if not 200 <= response.status < 300 or (code not in (None, 0) and not accepted_audit):
                    message = data.get("message", "request failed") if isinstance(data, dict) else "request failed"
                    raise QQOfficialAPIError(response.status, code, self._safe_text(message, token), trace_id)
                return data

    async def reply(self, ctx: Context, text: str) -> Any:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("reply text must be non-empty")
        payload = {"content": text}
        if ctx.resolve(MESSAGE_TYPE) in ("group", "private"):
            payload["msg_type"] = 0
        return await self.reply_message(ctx, json=payload)

    async def send_message(self, scene: str, target: str, *, json: dict[str, Any]) -> Any:
        """Send a native message body, including Markdown, keyboard or media."""
        self._validate_message(json)
        return await self.request("POST", _scene_path(scene, target) + "/messages", json=json)

    @staticmethod
    def _validate_message(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("QQ message body must be a JSON object")
        if payload.get("msg_id") and payload.get("event_id"):
            raise ValueError("QQ msg_id and event_id are mutually exclusive")
        if payload.get("is_wakeup") and (payload.get("msg_id") or payload.get("event_id")):
            raise ValueError("QQ wakeup messages cannot carry msg_id or event_id")

    async def reply_message(self, ctx: Context, *, json: dict[str, Any]) -> Any:
        """Bind a native body to this message/event; allocate a sequence before I/O."""
        self._validate_message(json)
        if any(key in json for key in ("msg_id", "event_id", "msg_seq", "is_wakeup")):
            raise ValueError("QQ reply reference and sequence are supplied by the context")
        scene = ctx.resolve(MESSAGE_TYPE)
        field = _REPLY_FIELDS.get(scene)
        if field is None:
            raise ValueError("QQ passive reply requires a supported scene")
        target = ctx.resolve(field)
        event = ctx.raw.get("t")
        if event in MESSAGE_EVENTS:
            reference = {"msg_id": ctx.resolve(MESSAGE_ID)}
        elif event in REPLY_EVENTS:
            reference = {"event_id": ctx.resolve(EVENT_ID)}
        else:
            raise ValueError("QQ event does not support a context reply; use send_message with an explicit target")
        identifier = next(iter(reference.values()))
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("QQ passive reply requires a message or event ID")
        _scene_path(scene, target)
        payload = json | reference
        if scene in ("group", "private"):
            sequence = (ctx.resolve(_REPLY_SEQ) if ctx.has(_REPLY_SEQ) else 0) + 1
            ctx.put(_REPLY_SEQ, sequence)
            payload["msg_seq"] = sequence
        return await self.send_message(scene, target, json=payload)

    async def send_stream_message(self, user_openid: str, *, json: dict[str, Any]) -> Any:
        """One C2C stream fragment; caller retains id, index and msg_seq across fragments."""
        self._validate_message(json)
        return await self.request(
            "POST", _scene_path("private", user_openid) + "/stream_messages", json=json,
        )

    async def delete_message(self, scene: str, target: str, message_id: str, *, hidetip: bool = False) -> Any:
        path = _scene_path(scene, target) + "/messages/" + _identifier(message_id)
        if scene in ("channel", "direct"):
            path += "?hidetip=" + ("true" if hidetip else "false")
        return await self.request("DELETE", path)

    async def acknowledge_interaction(self, interaction_id: str, code: int = 0) -> Any:
        """Use INTERACTION_CREATE.d.id, without the gateway event-name prefix."""
        if type(code) is not int or not 0 <= code <= 5:
            raise ValueError("QQ interaction code must be an integer from 0 to 5")
        return await self.request("PUT", "/interactions/" + _identifier(interaction_id), json={"code": code})

    async def upload_media(
        self, scene: str, target: str, *, file_type: int,
        url: str | None = None, upload_id: str | None = None, file_name: str | None = None,
    ) -> Any:
        """Register a public URL or merge completed parts; never send implicitly."""
        path = self._media_path(scene, target)
        if type(file_type) is not int or file_type not in (1, 2, 3, 4):
            raise ValueError("QQ file_type must be 1 (image), 2 (video), 3 (voice) or 4 (file)")
        if bool(url) == bool(upload_id):
            raise ValueError("QQ upload requires exactly one of url or upload_id")
        if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
            raise ValueError("QQ media URL must use HTTP or HTTPS")
        body = {"file_type": file_type, "srv_send_msg": False}
        body["url" if url else "upload_id"] = url or upload_id
        if file_name is not None:
            body["file_name"] = file_name
        return await self.request("POST", path + "/files", json=body)

    @staticmethod
    def _media_path(scene: str, target: str) -> str:
        if scene not in ("group", "private"):
            raise ValueError("QQ file_info uploads require a group or private scene")
        return _scene_path(scene, target)

    async def put_upload_part(
        self, presigned_url: str, data: bytes | AsyncIterable[bytes], *, size: int | None = None,
    ) -> None:
        """PUT one part without QQ credentials, sharing the client's HTTP budget."""
        if not isinstance(presigned_url, str) or not presigned_url.startswith(("https://", "http://")):
            raise ValueError("QQ upload part URL must use HTTP or HTTPS")
        if size is not None and (type(size) is not int or size <= 0):
            raise ValueError("QQ upload part size must be a positive integer")
        headers = {"Content-Length": str(size)} if size is not None else {}
        async with self._call():
            async with self._slots:
                async with self._session.put(presigned_url, data=data, headers=headers, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        raise QQOfficialAPIError(response.status, None, "upload part failed")
                    # COS returns an empty/XML response, not QQ API JSON.
                    received = 0
                    async for chunk in response.content.iter_chunked(65536):
                        received += len(chunk)
                        if received > 65536:
                            raise QQOfficialClientError("QQ upload part response exceeds 64 KiB")

    async def upload_file(self, scene: str, target: str, path: str | Path, *, file_type: int = 4) -> Any:
        """Upload a local file in bounded, sequential parts; return native file_info/ttl."""
        base = self._media_path(scene, target)
        if type(file_type) is not int or file_type not in (1, 2, 3, 4):
            raise ValueError("QQ file_type must be 1, 2, 3 or 4")
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= _MAX_FILE_SIZE:
            raise ValueError("QQ upload requires a regular, non-empty file of at most 200 MiB")
        async with self._call():
            info = await asyncio.to_thread(_file_hashes, path)
            prepared = await self.request("POST", base + "/upload_prepare", json=info | {"file_type": file_type})
            if not isinstance(prepared, dict) or not isinstance(prepared.get("upload_id"), str) or not prepared["upload_id"]:
                raise QQOfficialClientError("QQ upload_prepare returned no upload_id")
            parts = prepared.get("parts")
            if not isinstance(parts, list) or not parts or len(parts) > 4096:
                raise QQOfficialClientError("QQ upload_prepare returned invalid parts")
            uploaded = 0
            with path.open("rb") as stream:
                for index, part in enumerate(parts):
                    if not isinstance(part, dict) or type(part.get("index")) is not int or part["index"] != index:
                        raise QQOfficialClientError("QQ upload parts must have consecutive indices")
                    try:
                        block_size = int(part.get("block_size", prepared.get("block_size")))
                    except (TypeError, ValueError):
                        raise QQOfficialClientError("QQ upload_prepare returned invalid block_size") from None
                    if not 0 < block_size <= _MAX_FILE_SIZE:
                        raise QQOfficialClientError("QQ upload_prepare returned invalid block_size")
                    # aiohttp streams this iterator, keeping at most 64 KiB in memory.
                    remaining = min(block_size, int(info["file_size"]) - uploaded)
                    if remaining <= 0:
                        raise QQOfficialClientError("QQ upload_prepare returned excess parts")
                    part_md5 = hashlib.md5()
                    part_size = remaining
                    async def chunks():
                        nonlocal remaining
                        while remaining:
                            chunk = await asyncio.to_thread(stream.read, min(65536, remaining))
                            if not chunk:
                                raise QQOfficialClientError("QQ local file changed during upload")
                            part_md5.update(chunk)
                            remaining -= len(chunk)
                            yield chunk
                    url = part.get("presigned_url")
                    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                        raise QQOfficialClientError("QQ upload_prepare returned invalid presigned_url")
                    # Signed COS URLs may reject chunked encoding.
                    await self.put_upload_part(url, chunks(), size=part_size)
                    if remaining:
                        raise QQOfficialClientError("QQ upload part body was not consumed")
                    await self.request("POST", base + "/upload_part_finish", json={
                        "upload_id": prepared["upload_id"], "part_index": index,
                        "block_size": str(part_size), "md5": part_md5.hexdigest(),
                    })
                    uploaded += part_size
            if uploaded != int(info["file_size"]):
                raise QQOfficialClientError("QQ upload_prepare parts do not cover the file")
            return await self.upload_media(scene, target, file_type=file_type,
                                           upload_id=prepared["upload_id"], file_name=path.name)

    async def reply_text(self, ctx: Context, text: str) -> Any:
        return await self.reply(ctx, text)
