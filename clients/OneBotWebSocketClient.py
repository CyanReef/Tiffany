from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Hashable, Mapping


class OneBotClientError(RuntimeError):
    """Base class for failures visible to OneBot API callers."""


class OneBotClientNotRunningError(OneBotClientError):
    pass


class PendingLimitExceeded(OneBotClientError):
    def __init__(self, limit: int):
        super().__init__(f"OneBot pending request limit reached ({limit})")
        self.limit = limit


class OneBotCallTimeoutError(OneBotClientError):
    def __init__(self, action: str, echo: Hashable, timeout: float):
        super().__init__(
            f"OneBot action {action!r} timed out after {timeout:g} seconds"
        )
        self.action = action
        self.echo = echo
        self.timeout = timeout


class OneBotConnectionLostError(OneBotClientError):
    def __init__(self, connection_id: str, reason: str = "connection closed"):
        super().__init__(f"OneBot connection {connection_id!r} disconnected: {reason}")
        self.connection_id = connection_id
        self.reason = reason


class OneBotSendError(OneBotClientError):
    def __init__(self, action: str, echo: Hashable):
        super().__init__(f"failed to send OneBot action {action!r}")
        self.action = action
        self.echo = echo


class OneBotProtocolError(OneBotClientError):
    def __init__(self, message: str, raw: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.raw = raw


class OneBotActionError(OneBotClientError):
    def __init__(self, result: "OneBotCallResult"):
        super().__init__(
            f"OneBot action {result.action!r} failed with retcode "
            f"{result.retcode!r}: {result.message or result.wording or 'unknown error'}"
        )
        self.result = result


@dataclass(frozen=True, slots=True)
class OneBotCallResult:
    action: str
    echo: Hashable
    status: str | None
    retcode: int | None
    data: Any
    message: str | None
    wording: str | None
    raw: Mapping[str, Any]
    duration: float

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.retcode in (None, 0)


@dataclass(frozen=True, slots=True)
class SendMessageResult:
    message_id: int | str | None
    call: OneBotCallResult


@dataclass(slots=True)
class _PendingCall:
    action: str
    started_at: float
    future: asyncio.Future[dict[str, Any]]


class OneBotWebSocketClient:
    def __init__(
        self,
        ws,
        *,
        connection_id: str = "onebot-standalone",
        generation: int = 0,
        call_timeout: float = 30.0,
        pending_limit: int = 256,
        metrics: object | None = None,
        platform: str = "onebot",
        adapter_id: str = "onebot_websocket",
    ):
        if call_timeout <= 0:
            raise ValueError("call_timeout must be greater than zero")
        if pending_limit < 1:
            raise ValueError("pending_limit must be at least 1")
        self.ws = ws
        self.connection_id = connection_id
        self.generation = generation
        self.call_timeout = call_timeout
        self.pending_limit = pending_limit
        self._metrics = metrics
        self._metric_labels = {"platform": platform, "adapter": adapter_id}
        self._pending: dict[Hashable, _PendingCall] = {}
        self._pending_empty = asyncio.Event()
        self._pending_empty.set()
        self._next_echo = 1
        self._running = True
        self._runtime: object | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def running(self) -> bool:
        return self._running

    async def setup(self, runtime: object) -> None:
        self._runtime = runtime
        if self._metrics is None:
            self._metrics = getattr(runtime, "metrics", None)

    async def start(self) -> None:
        self._running = True

    async def stop(self, mode: str = "drain") -> None:
        if mode not in ("drain", "abort"):
            raise ValueError("stop mode must be 'drain' or 'abort'")
        if mode == "abort":
            self.disconnect("client aborted")

    async def wait_pending(self) -> None:
        """Wait without polling until every accepted action call has settled."""

        await self._pending_empty.wait()

    async def teardown(self) -> None:
        self.disconnect("client torn down")
        self._runtime = None

    async def reply_text(self, ctx, text: str) -> SendMessageResult:
        return await self.reply(ctx, text)

    async def reply(self, ctx, text: str) -> SendMessageResult:
        if not self._running:
            raise OneBotConnectionLostError(
                self.connection_id,
                "event connection is no longer active",
            )
        return await self.send_text(ctx.raw, text)

    async def send_text(
        self,
        raw: dict[str, Any],
        text: str,
        *,
        timeout: float | None = None,
    ) -> SendMessageResult:
        params: dict[str, Any] = {
            "message_type": raw.get("message_type"),
            "message": text,
        }

        if raw.get("message_type") == "group":
            params["group_id"] = raw.get("group_id")
        elif raw.get("message_type") == "private":
            params["user_id"] = raw.get("user_id")
        else:
            raise ValueError(f"unsupported message_type: {raw.get('message_type')}")

        call = await self.call("send_msg", params, timeout=timeout)
        data = call.data
        message_id = data.get("message_id") if isinstance(data, dict) else None
        return SendMessageResult(message_id=message_id, call=call)

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> OneBotCallResult:
        if not self._running:
            raise OneBotClientNotRunningError("OneBot client is not running")
        effective_timeout = self.call_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if len(self._pending) >= self.pending_limit:
            self._inc("onebot_calls_rejected_total", reason="pending_limit")
            raise PendingLimitExceeded(self.pending_limit)

        echo = f"{self.generation}:{self._next_echo}"
        self._next_echo += 1
        loop = asyncio.get_running_loop()
        pending = _PendingCall(
            action=action,
            started_at=time.monotonic(),
            future=loop.create_future(),
        )
        self._pending[echo] = pending
        self._pending_empty.clear()
        self._set_pending_metric()
        self._inc("onebot_calls_started_total")
        payload = {
            "action": action,
            "params": {} if params is None else params,
            "echo": echo,
        }
        failure_reason: str | None = None
        try:
            try:
                async with asyncio.timeout(effective_timeout):
                    await self.ws.send(json.dumps(payload, ensure_ascii=False))
                    raw = await asyncio.shield(pending.future)
            except asyncio.CancelledError:
                failure_reason = "cancelled"
                raise
            except TimeoutError as error:
                failure_reason = "timeout"
                self._inc("onebot_calls_failed_total", reason="timeout")
                raise OneBotCallTimeoutError(
                    action,
                    echo,
                    effective_timeout,
                ) from error
            except Exception as error:
                if isinstance(error, OneBotClientError):
                    failure_reason = type(error).__name__
                    raise
                failure_reason = "send"
                raise OneBotSendError(action, echo) from error

            try:
                result = self._make_result(action, echo, raw, pending.started_at)
            except OneBotClientError as error:
                failure_reason = type(error).__name__
                raise
            if not result.ok:
                failure_reason = "response"
                self._inc("onebot_calls_failed_total", reason="response")
                raise OneBotActionError(result)
            self._inc("onebot_calls_completed_total")
            return result
        finally:
            self._observe(
                "onebot_call_duration_seconds",
                time.monotonic() - pending.started_at,
            )
            if failure_reason not in (None, "timeout", "response"):
                self._inc(
                    "onebot_calls_failed_total",
                    reason=failure_reason,
                )
            current = self._pending.get(echo)
            if current is pending:
                self._pending.pop(echo, None)
            if not pending.future.done():
                pending.future.cancel()
            elif not pending.future.cancelled():
                pending.future.exception()
            self._mark_pending_empty()
            self._set_pending_metric()

    def handle_response(self, raw: dict[str, Any]) -> bool:
        """Consume every echo frame, resolving known calls and counting orphans."""

        if "echo" not in raw:
            return False
        echo = raw["echo"]
        try:
            pending = self._pending.get(echo)
        except TypeError:
            pending = None
        if pending is None:
            self._inc("onebot_orphan_responses_total")
            return True
        if not _valid_response(raw):
            if not pending.future.done():
                pending.future.set_exception(
                    OneBotProtocolError("invalid OneBot action response", raw)
                )
            return True
        if not pending.future.done():
            pending.future.set_result(raw)
        return True

    def disconnect(self, reason: str = "connection closed") -> None:
        if not self._running and not self._pending:
            return
        self._running = False
        error = OneBotConnectionLostError(self.connection_id, reason)
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
        self._mark_pending_empty()
        self._set_pending_metric()
        self._inc("onebot_disconnects_total")

    @staticmethod
    def _make_result(
        action: str,
        echo: Hashable,
        raw: dict[str, Any],
        started_at: float,
    ) -> OneBotCallResult:
        retcode = raw.get("retcode")
        return OneBotCallResult(
            action=action,
            echo=echo,
            status=raw.get("status") if isinstance(raw.get("status"), str) else None,
            retcode=retcode if isinstance(retcode, int) else None,
            data=raw.get("data"),
            message=raw.get("message") if isinstance(raw.get("message"), str) else None,
            wording=raw.get("wording") if isinstance(raw.get("wording"), str) else None,
            raw=raw,
            duration=time.monotonic() - started_at,
        )

    def _inc(self, name: str, *, reason: str | None = None) -> None:
        if self._metrics is None:
            return
        labels = self._metric_labels
        if reason is not None:
            labels = {**labels, "reason": reason}
        self._metrics.inc(name, labels=labels)

    def _observe(self, name: str, value: float) -> None:
        if self._metrics is not None:
            self._metrics.observe(name, value, labels=self._metric_labels)

    def _set_pending_metric(self) -> None:
        if self._metrics is not None:
            self._metrics.set(
                "onebot_pending_requests",
                len(self._pending),
                labels=self._metric_labels,
            )

    def _mark_pending_empty(self) -> None:
        if not self._pending:
            self._pending_empty.set()


# Compatibility aliases retained for code written against the prototype.
OneBotPendingLimitError = PendingLimitExceeded
OneBotDisconnectedError = OneBotConnectionLostError
OneBotResponseError = OneBotActionError


def _valid_response(raw: Mapping[str, Any]) -> bool:
    status = raw.get("status")
    retcode = raw.get("retcode")
    return status in ("ok", "failed") and (
        isinstance(retcode, int) and not isinstance(retcode, bool)
    )
