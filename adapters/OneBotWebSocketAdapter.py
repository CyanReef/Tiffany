from __future__ import annotations

import asyncio
import inspect
import json
import logging
import hmac
import math
import sys
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from websockets.asyncio.server import serve

from clients import OneBotWebSocketClient, create_client
from core import Envelope
from settings import is_loopback

from .onebot_fields import detect_event_kind, register_onebot_fields


logger = logging.getLogger(__name__)
StopMode = Literal["drain", "abort"]


class OneBotWebSocketAdapter:
    """OneBot reverse WebSocket transport with one active connection."""

    def __init__(
        self,
        host: str,
        port: int,
        platform: str,
        *,
        adapter_id: str | None = None,
        workers: int | None = None,
        call_timeout: float = 30.0,
        pending_limit: int = 256,
        token: str | None = None,
    ):
        if not isinstance(host, str) or type(port) is not int or not 0 <= port <= 65535 or not isinstance(
            platform, str
        ):
            raise TypeError("host, port, and platform have invalid types")
        if workers is not None and (type(workers) is not int or workers < 1):
            raise ValueError("workers must be a positive integer or None")
        if (type(call_timeout) not in (float, int) or not math.isfinite(call_timeout)
                or call_timeout <= 0 or type(pending_limit) is not int or pending_limit < 1):
            raise ValueError("call_timeout must be positive and pending_limit at least 1")
        if token is not None and (not isinstance(token, str) or not token or any(ord(c) < 32 for c in token)):
            raise ValueError("OneBot Token must be a non-empty string without control characters")
        if not token and not is_loopback(host):
            raise ValueError("non-loopback OneBot binding requires a Token")

        self.host = host
        self.port = port
        self.platform = platform
        self.adapter_id = adapter_id or f"onebot_websocket:{platform}"
        self.workers = workers
        self.call_timeout = call_timeout
        self.pending_limit = pending_limit
        self._token = token
        self._providers = ()

        self.runtime: object | None = None
        self._server: object | None = None
        self._active_ws: object | None = None
        self._active_client: OneBotWebSocketClient | None = None
        self._generation = 0
        self._accepting_events = False
        self._started = False
        self._stopping = False
        self._last_overload_log = float("-inf")

    @property
    def active_client(self) -> OneBotWebSocketClient | None:
        return self._active_client

    @property
    def connection_generation(self) -> int:
        return self._generation

    @property
    def ready(self) -> bool:
        return (self._active_ws is not None and self._active_client is not None
                and self._active_client.running and self._accepting_events and not self._stopping)

    def _authenticate(self, connection, request):
        if self._token:
            values = request.headers.get_all("Authorization")
            supplied = values[0] if len(values) == 1 else ""
            if not hmac.compare_digest(supplied.encode(), f"Bearer {self._token}".encode()):
                return connection.respond(401, "Unauthorized\n")
        return None

    async def setup(self, runtime: object) -> None:
        if self.runtime is runtime:
            return
        if self.runtime is not None:
            raise RuntimeError("adapter is already set up with another runtime")
        self.runtime = runtime
        bot = getattr(runtime, "bot", None)
        if bot is not None:
            self._providers = register_onebot_fields(bot, self.platform)
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is not None and hasattr(scheduler, "configure_adapter"):
            scheduler.configure_adapter(self.adapter_id, self.workers)

    async def start(self) -> None:
        if self._started:
            return
        if self.runtime is None:
            raise RuntimeError("adapter must be set up before start")
        self._server = await serve(self.handle, self.host, self.port,
                                   process_request=self._authenticate)
        self._accepting_events = True
        self._stopping = False
        self._started = True
        logger.info("listening on ws://%s:%s/ws", self.host, self.port)

    async def stop(self, mode: StopMode = "drain") -> None:
        if mode not in ("drain", "abort"):
            raise ValueError("stop mode must be 'drain' or 'abort'")
        self._accepting_events = False
        self._stopping = True

        server = self._server
        if server is not None:
            server.close(close_connections=False)
            if self._active_ws is None:
                await server.wait_closed()
        if mode == "drain" and self.runtime is not None:
            # Keep receiving responses and let accepted hooks issue calls until
            # this adapter's work has drained. Runtime owns the outer deadline.
            await self.runtime.scheduler.wait_adapter_idle(self.adapter_id)
        client = self._active_client
        if client is not None:
            await client.stop(mode)
            if mode == "drain":
                await client.wait_pending()
        await self._close_active(1012 if mode == "abort" else 1001, f"runtime {mode}")
        self._started = False

    async def teardown(self) -> None:
        self._accepting_events = False
        self._stopping = True
        if self.runtime is not None:
            await self.runtime.tasks.cancel_owner(self, grace=1.0)
        await self._close_active(1001, "runtime teardown")
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        for handle in self._providers:
            handle.revoke()
        self._providers = ()
        self.runtime = None
        self._started = False

    async def handle(self, ws) -> None:
        if self.runtime is None or not self._started or self._stopping:
            await self._close_ws(ws, 1013, "OneBot adapter is not accepting connections")
            self._inc("onebot_connections_rejected_total", reason="not_running")
            return
        if self._active_ws is not None or self._active_client is not None:
            await self._close_ws(ws, 1013, "another OneBot connection is active")
            self._inc("onebot_connections_rejected_total", reason="active_connection")
            return

        self._generation += 1
        connection_id = f"{self.adapter_id}:{self._generation}:{uuid4().hex[:12]}"
        # Reserve the sole connection slot before any await below.
        self._active_ws = ws
        client = create_client(
            "onebot_websocket",
            ws=ws,
            connection_id=connection_id,
            generation=self._generation,
            call_timeout=self.call_timeout,
            pending_limit=self.pending_limit,
            metrics=getattr(self.runtime, "metrics", None),
            platform=self.platform,
            adapter_id=self.adapter_id,
        )
        assert isinstance(client, OneBotWebSocketClient)
        self._active_client = client
        try:
            if self.runtime is not None:
                await client.setup(self.runtime)
            await client.start()
            self._inc("onebot_connections_total")
            logger.info("OneBot client connected connection_id=%s", connection_id)
            frames = 0
            async for message in ws:
                await self._process_frame(message, client, connection_id)
                frames += 1
                if frames == 64:
                    frames = 0
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot connection failed connection_id=%s", connection_id)
        finally:
            client.disconnect("websocket disconnected")
            await client.teardown()
            if self._active_ws is ws and self._active_client is client:
                self._active_ws = None
                self._active_client = None
            logger.info("OneBot client disconnected connection_id=%s", connection_id)

    async def _process_frame(
        self,
        message: str | bytes,
        client: OneBotWebSocketClient,
        connection_id: str | None = None,
    ) -> None:
        try:
            raw = self._decode_event(message)
        except Exception:
            logger.exception("failed to decode OneBot frame")
            self._inc("onebot_frames_failed_total", reason="decode")
            return

        # Every echo frame belongs to the API-response plane, including
        # malformed or late responses. It must never reach event hooks.
        if client.handle_response(raw):
            return
        if not self._accepting_events:
            self._inc("events_rejected_total", reason="adapter_stopping")
            return

        envelope = Envelope(
            platform=self.platform,
            raw=raw,
            client=client,
            kind=detect_event_kind(raw),
            adapter_id=self.adapter_id,
            connection_id=connection_id or client.connection_id,
            session_id=self._session_id(raw),
            admission_bytes=2048 + 12 * sys.getsizeof(message),
        )
        try:
            self._submit_event(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to submit OneBot event event_id=%s connection_id=%s",
                envelope.event_id,
                envelope.connection_id,
            )

    def _submit_event(self, envelope: Envelope):
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("adapter is not set up")
        result = runtime.submit(envelope, reject=False)
        if result is None:
            self._inc("onebot_events_dropped_total", reason="overload")
            now = monotonic()
            if now - self._last_overload_log >= 1.0:
                self._last_overload_log = now
                logger.warning("OneBot event rejected event_id=%s reason=overload", envelope.event_id)
        return result

    async def _close_active(self, code: int, reason: str) -> None:
        client = self._active_client
        if client is not None:
            client.disconnect(reason)
        ws = self._active_ws
        if ws is not None:
            await self._close_ws(ws, code, reason)

    @staticmethod
    async def _close_ws(ws, code: int, reason: str) -> None:
        close = getattr(ws, "close", None)
        if close is None:
            return
        result = close(code=code, reason=reason)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _decode_event(message: str | bytes) -> dict[str, Any]:
        raw = json.loads(message)
        if not isinstance(raw, dict):
            raise ValueError("OneBot frame must decode to a JSON object")
        return raw

    @staticmethod
    def _session_id(raw: dict[str, Any]) -> str | None:
        self_id = raw.get("self_id")
        message_type = raw.get("message_type")
        if message_type == "group" and raw.get("group_id") is not None:
            return f"{self_id}:group:{raw['group_id']}"
        if message_type == "private" and raw.get("user_id") is not None:
            return f"{self_id}:private:{raw['user_id']}"
        return None

    def _inc(self, name: str, *, reason: str | None = None) -> None:
        metrics = getattr(self.runtime, "metrics", None)
        if metrics is None:
            return
        labels = {"platform": self.platform, "adapter": self.adapter_id}
        if reason is not None:
            labels["reason"] = reason
        metrics.inc(name, labels=labels)
