"""In-memory WebSocket doubles and an adapter runtime stub."""

import asyncio

from core import MetricRegistry, TaskRegistry


class RecordingWebSocket:
    """Record outbound payloads without a network connection."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class FakeIncomingWebSocket(RecordingWebSocket):
    """Yield predefined transport frames to an adapter."""

    def __init__(self, incoming):
        super().__init__()
        self.incoming = incoming

    def __aiter__(self):
        self._incoming_iterator = iter(self.incoming)
        return self

    async def __anext__(self):
        try:
            return next(self._incoming_iterator)
        except StopIteration:
            raise StopAsyncIteration


class FakeWebSocket:
    """Control incoming frames, send failures, send blocking and close calls."""

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []
        self.send_error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.clients = []

    async def send(self, payload: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(payload)
        if self.gate is not None:
            await self.gate.wait()

    async def close(self, code=1000, reason="") -> None:
        self.closed.append((code, reason))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class FakeRuntime:
    """Record admitted envelopes while supervising adapter tasks."""

    def __init__(self):
        self.tasks = TaskRegistry()
        self.metrics = MetricRegistry()
        self.envelopes = []
        self.emit_result = object()
        self.emit_gate: asyncio.Event | None = None

    async def emit_from_adapter(self, envelope, *, adapter_id, reject):
        self.envelopes.append(envelope)
        if self.emit_gate is not None:
            await self.emit_gate.wait()
        return self.emit_result
