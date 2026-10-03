"""One QQ account, one gateway connection, and raw-first event admission."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import OrderedDict
from typing import Any, Literal

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from clients.QQOfficialClient import QQOfficialAPIError, QQOfficialClient, QQOfficialTransportError
from core import Envelope, RuntimeNotRunningError, RuntimeState

from .qqofficial_fields import MESSAGE_EVENTS, detect_event_kind, event_data, register_qqofficial_fields


logger = logging.getLogger(__name__)


class QQOfficialGatewayError(RuntimeError):
    """Permanent gateway authentication, permission or protocol failure."""


class _Reconnect(Exception):
    def __init__(self, *, clear_session: bool = False):
        self.clear_session = clear_session


class QQOfficialWebSocketAdapter:
    INTENTS = 1 << 25
    MAX_QUEUE = 16
    MAX_SIZE = 1024 * 1024
    HANDSHAKE_TIMEOUT = 30.0

    def __init__(
        self, app_id: str, app_secret: str, platform: str = "qq_official", *,
        sandbox: bool = False, workers: int = 4, call_timeout: float = 30.0,
        adapter_id: str | None = None, client: QQOfficialClient | None = None,
        dedup_limit: int = 4096, dedup_ttl: float = 3600.0,
    ):
        if not isinstance(platform, str) or not platform:
            raise ValueError("platform must be a non-empty string")
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("QQ adapter workers must be between 1 and 4")
        if (type(dedup_limit) is not int or dedup_limit < 1
                or not math.isfinite(dedup_ttl) or dedup_ttl <= 0):
            raise ValueError("dedup_limit and dedup_ttl must be positive")
        self.client = client or QQOfficialClient(
            app_id, app_secret, sandbox=sandbox, call_timeout=call_timeout,
        )
        if self.client.app_id != app_id:
            raise ValueError("adapter and client must use the same app_id")
        self.app_id = app_id
        self.platform = platform
        self.adapter_id = adapter_id or f"qqofficial_websocket:{app_id}"
        self.workers = workers
        self.dedup_limit = dedup_limit
        self.dedup_ttl = dedup_ttl
        self.runtime: Any = None
        self.self_id: str | None = None
        self._session_id: str | None = None
        self._received_seq: int | None = None
        self._admitted_seq: int | None = None
        self._generation = 0
        self._ws: Any = None
        self._runner: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._awaiting_ack = False
        self._auth_failures = 0
        self._invalid_sessions = 0
        self._providers = ()
        self._connected = False
        self._seen: OrderedDict[tuple[str, str, str], float] = OrderedDict()

    @property
    def ready(self) -> bool:
        return self._connected and self._ws is not None and not self._stop.is_set()

    async def setup(self, runtime: Any) -> None:
        if self.runtime is runtime:
            return
        if self.runtime is not None:
            raise RuntimeError("QQ adapter already belongs to another runtime")
        handles = register_qqofficial_fields(runtime.bot, self.platform, lambda: self.self_id)
        try:
            runtime.scheduler.configure_adapter(self.adapter_id, self.workers)
            await self.client.setup(runtime)
        except BaseException:
            for handle in handles:
                handle.revoke()
            raise
        self._providers = handles
        self.runtime = runtime

    async def start(self) -> None:
        if self._runner is not None:
            return
        if self.runtime is None:
            raise RuntimeError("QQ adapter must be set up before start")
        self._stop.clear()
        try:
            await self.client.start()
            gateway = await self.client.request("GET", "/gateway")
            url = gateway.get("url") if isinstance(gateway, dict) else None
            if not isinstance(url, str) or not url.startswith(("wss://", "ws://")):
                raise QQOfficialGatewayError("QQ /gateway returned no WebSocket URL; check account transport permissions")
            self._runner = self.runtime.tasks.spawn(
                self._run(url), name=f"{self.adapter_id}:gateway", owner=self,
            )
        except BaseException:
            await self.client.stop("abort")
            raise

    async def stop(self, mode: Literal["drain", "abort"] = "drain") -> None:
        if mode not in ("drain", "abort"):
            raise ValueError("stop mode must be 'drain' or 'abort'")
        self._stop.set()
        self._connected = False
        if self._ws is not None:
            await self._ws.close(code=1000, reason="Tiffany stopping")
        runner = self._runner
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self._runner = None
        await self.client.stop(mode)

    async def teardown(self) -> None:
        await self.stop("abort")
        for handle in self._providers:
            handle.revoke()
        self._providers = ()
        self._seen.clear()
        self._clear_session()
        self.self_id = None

    async def _wait(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    def _clear_session(self) -> None:
        self._session_id = None
        self._received_seq = None
        self._admitted_seq = None

    async def _run(self, url: str) -> None:
        try:
            # Runtime starts its scheduler only after every adapter.start returns.
            while not self._stop.is_set() and self.runtime.state in (RuntimeState.STARTING, RuntimeState.SETUP):
                await self._wait(0.01)
            attempt = 0
            while not self._stop.is_set() and self.runtime.state == RuntimeState.RUNNING:
                started = time.monotonic()
                code = None
                try:
                    await self._connection(url)
                except _Reconnect as error:
                    if error.clear_session:
                        self._clear_session()
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd is not None else None
                    if code in (4914, 4915):
                        raise QQOfficialGatewayError(f"QQ gateway permission denied (close {code}); check account availability and intents") from None
                    if code == 4004:
                        self._auth_failures += 1
                        if self._auth_failures > 1:
                            raise QQOfficialGatewayError("QQ gateway authentication failed after token refresh (4004)") from None
                        self.client.invalidate_access_token()
                    if code in (4006, 4007, 4009) or (code is not None and 4900 <= code <= 4913):
                        self._clear_session()
                except InvalidStatus as error:
                    code = error.response.status_code
                    if code < 500 and code != 429:
                        raise QQOfficialGatewayError(f"QQ WebSocket handshake rejected (HTTP {code})") from None
                except QQOfficialAPIError as error:
                    if error.status != 429 and error.status < 500:
                        raise
                    code = error.status
                except (OSError, TimeoutError, QQOfficialTransportError):
                    pass
                if self._stop.is_set() or self.runtime.state != RuntimeState.RUNNING:
                    return
                if time.monotonic() - started >= 30:
                    attempt = 0
                delay = self._retry_delay(attempt, code)
                attempt = min(attempt + 1, 6)
                logger.warning("QQ gateway disconnected (code=%s); reconnecting in %.1fs", code, delay)
                await self._wait(delay)
        except asyncio.CancelledError:
            # Intentional stop is normal completion for a critical owned task.
            if not self._stop.is_set():
                raise

    @staticmethod
    def _retry_delay(attempt: int, code: int | None) -> float:
        if code in (4008, 429):
            return 60.0 + random.uniform(0, 5)
        return random.uniform(1, min(60.0, 2 ** (min(attempt, 6) + 1)))

    async def _connection(self, url: str) -> None:
        token = await self.client.get_access_token()
        async with connect(
            url, ping_interval=None, max_queue=self.MAX_QUEUE, max_size=self.MAX_SIZE,
            open_timeout=self.HANDSHAKE_TIMEOUT, close_timeout=1,
        ) as ws:
            self._ws = ws
            self._generation += 1
            self._awaiting_ack = False
            try:
                try:
                    async with asyncio.timeout(self.HANDSHAKE_TIMEOUT):
                        hello = self._decode(await ws.recv())
                except ValueError:
                    raise QQOfficialGatewayError("QQ gateway first frame is not a valid HELLO") from None
                interval = event_data(hello).get("heartbeat_interval")
                if (hello.get("op") != 10 or type(interval) not in (int, float)
                        or not math.isfinite(interval) or interval <= 0):
                    raise QQOfficialGatewayError("QQ gateway sent an invalid HELLO/heartbeat_interval")
                if self._session_id is not None:
                    auth = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._admitted_seq}}
                else:
                    auth = {"op": 2, "d": {"token": f"QQBot {token}", "intents": self.INTENTS, "shard": [0, 1]}}
                await self._send(ws, auth)
                self._heartbeat_task = self.runtime.tasks.spawn(
                    self._heartbeat(ws, interval / 1000), name=f"{self.adapter_id}:heartbeat",
                    owner=self,
                )
                deadline = asyncio.get_running_loop().time() + self.HANDSHAKE_TIMEOUT
                authorized = False
                while not self._stop.is_set():
                    if authorized:
                        frame = await ws.recv()
                    else:
                        async with asyncio.timeout_at(deadline):
                            frame = await ws.recv()
                    try:
                        raw = self._decode(frame)
                    except ValueError:
                        logger.warning("ignoring malformed QQ gateway frame")
                        continue
                    if not authorized and self._session_id is None and raw.get("op") == 0 and raw.get("t") != "READY":
                        raise QQOfficialGatewayError("QQ dispatched an event before READY")
                    await self._handle(raw, ws)
                    if raw.get("op") == 0 and raw.get("t") in ("READY", "RESUMED"):
                        authorized = True
            finally:
                self._connected = False
                self._ws = None
                heartbeat, self._heartbeat_task = self._heartbeat_task, None
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    @staticmethod
    def _decode(frame: str | bytes) -> dict[str, Any]:
        raw = json.loads(frame)
        if not isinstance(raw, dict) or type(raw.get("op")) is not int:
            raise ValueError("QQ gateway frame must be an object with an opcode")
        return raw

    @staticmethod
    async def _send(ws: Any, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _send_heartbeat(self, ws: Any) -> None:
        self._awaiting_ack = True
        await self._send(ws, {"op": 1, "d": self._received_seq})

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        try:
            while not self._stop.is_set():
                await self._wait(interval)
                if self._stop.is_set():
                    return
                if self._awaiting_ack:
                    await ws.close(code=4000, reason="heartbeat ACK timeout")
                    return
                await self._send_heartbeat(ws)
        except ConnectionClosed:
            pass
        except OSError:
            await ws.close(code=4000, reason="heartbeat send failed")
        except asyncio.CancelledError:
            if self._ws is ws and not self._stop.is_set():
                raise

    async def _handle(self, raw: dict[str, Any], ws: Any) -> None:
        sequence = raw.get("s")
        if type(sequence) is int:
            self._received_seq = sequence
        op = raw["op"]
        if op == 11:
            self._awaiting_ack = False
        elif op == 1:
            await self._send_heartbeat(ws)
        elif op == 7:
            raise _Reconnect()
        elif op == 9:
            self._invalid_sessions += 1
            if self._invalid_sessions > 1:
                raise QQOfficialGatewayError("QQ gateway repeatedly rejected the session; check account and intents")
            raise _Reconnect(clear_session=raw.get("d") is not True)
        elif op == 0:
            event = raw.get("t")
            if not isinstance(event, str):
                logger.warning("ignoring QQ dispatch without an event type")
                return
            if event == "READY":
                data = event_data(raw)
                user = data.get("user")
                session_id = data.get("session_id")
                if (not isinstance(session_id, str) or not session_id
                        or not isinstance(user, dict)
                        or not isinstance(user.get("id"), str) or not user["id"]):
                    raise QQOfficialGatewayError("QQ READY has no session_id or bot user ID")
                self._session_id = session_id
                self.self_id = user["id"]
                self._auth_failures = self._invalid_sessions = 0
                self._admitted_seq = self._received_seq
                self._connected = True
                logger.info("QQ official connected (adapter=%s)", self.adapter_id)
            elif event == "RESUMED":
                self._auth_failures = self._invalid_sessions = 0
                self._admitted_seq = self._received_seq
                self._connected = True
                logger.info("QQ official session resumed (adapter=%s)", self.adapter_id)
            else:
                await self._admit(raw)

    def _message_key(self, raw: dict[str, Any]) -> tuple[str, str, str] | None:
        scene = MESSAGE_EVENTS.get(raw.get("t"))
        data = event_data(raw)
        author = data.get("author")
        if scene == "group":
            target = data.get("group_openid")
        elif scene == "private" and isinstance(author, dict):
            target = author.get("user_openid")
        else:
            target = None
        message_id = data.get("id")
        if scene and isinstance(target, str) and target and isinstance(message_id, str) and message_id:
            return scene, target, message_id
        return None

    async def _admit(self, raw: dict[str, Any]) -> None:
        if self._stop.is_set() or self.runtime.state != RuntimeState.RUNNING:
            raise _Reconnect()
        key = self._message_key(raw)
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values())) <= now:
            self._seen.popitem(last=False)
        if key is not None and key in self._seen:
            self._admitted_seq = self._received_seq
            self.runtime.metrics.inc("qqofficial_duplicates_total", labels={"platform": self.platform, "adapter": self.adapter_id})
            return
        scene = key[0] if key is not None else None
        target = key[1] if key is not None else None
        event_id = raw.get("id") or event_data(raw).get("id")
        metadata = {"event_id": event_id} if isinstance(event_id, str) and event_id else {}
        envelope = Envelope(
            self.platform, raw, client=self.client, kind=detect_event_kind(raw),
            adapter_id=self.adapter_id, connection_id=f"{self.adapter_id}:{self._generation}",
            session_id=f"{self.app_id}:{scene}:{target}" if key is not None else None,
            **metadata,
        )
        try:
            admitted = await self.runtime.emit(envelope, reject=False, wait=False)
        except RuntimeNotRunningError:
            raise _Reconnect() from None
        if admitted is None:
            logger.warning("QQ event rejected event_id=%s reason=overload; reconnecting", envelope.event_id)
            # Do not mark this message seen or move the RESUME checkpoint.
            raise _Reconnect()
        self._admitted_seq = self._received_seq
        if key is not None:
            self._seen[key] = now + self.dedup_ttl
            if len(self._seen) > self.dedup_limit:
                self._seen.popitem(last=False)
