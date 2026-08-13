from __future__ import annotations

import threading
import zlib
from collections import deque
from dataclasses import dataclass
from hashlib import blake2s
from os import urandom
from typing import Literal, TypeAlias


DispatchPhase: TypeAlias = Literal["predicate", "handler"]
TraceMode: TypeAlias = Literal["off", "sample", "all"]
DispatchResult: TypeAlias = Literal[
    "predicate_rejected",
    "completed",
    "stopped",
    "timeout",
    "error",
    "cancelled",
    "skipped",
]


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    """One bounded, payload-free hook execution record."""

    event_id: str | None
    connection_id: str | None
    session_id: str | None
    platform: str
    adapter_id: str | None
    hook_id: int
    hook_name: str
    registry_version: int
    phase: DispatchPhase
    result: DispatchResult
    duration_seconds: float
    queue_depth: int | None = None
    error_type: str | None = None
    hook_source: str | None = None
    hook_order: int = 0
    error_summary: str | None = None

    @property
    def source(self) -> str | None:
        return self.hook_source

    @property
    def order(self) -> int:
        return self.hook_order

    @property
    def execution_order(self) -> int:
        return self.hook_order

    @property
    def outcome(self) -> DispatchResult:
        """Compatibility alias for consumers that call the result outcome."""

        return self.result

    @property
    def duration(self) -> float:
        return self.duration_seconds

    @property
    def duration_ms(self) -> float:
        return self.duration_seconds * 1000.0


class TraceRecorder:
    """Opt-in, bounded in-memory dispatch tracing.

    Records deliberately contain no payload, exception object, traceback, or
    stack frame, so enabling diagnostics cannot retain event object graphs.
    """

    __slots__ = (
        "_capacity",
        "_mode",
        "_sample_rate",
        "_redaction_key",
        "_records",
        "_dropped",
        "_lock",
    )

    def __init__(
        self,
        *,
        enabled: bool = False,
        mode: TraceMode | None = None,
        sample_rate: float = 0.1,
        capacity: int = 256,
        max_records: int | None = None,
    ) -> None:
        if max_records is not None:
            capacity = max_records
        if capacity < 1:
            raise ValueError("trace capacity must be at least 1")
        if mode is None:
            mode = "all" if enabled else "off"
        _validate_mode(mode)
        _validate_sample_rate(sample_rate)
        self._capacity = capacity
        self._mode = mode
        self._sample_rate = float(sample_rate)
        self._redaction_key = urandom(16)
        self._records: deque[DispatchRecord] = deque(maxlen=capacity)
        self._dropped = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        # Dispatcher uses this lock-free read as the default-off hot-path
        # guard before allocating a DispatchRecord.
        return self._mode != "off"

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._lock:
            if value:
                if self._mode == "off":
                    self._mode = "all"
            else:
                self._mode = "off"

    @property
    def mode(self) -> TraceMode:
        return self._mode

    @mode.setter
    def mode(self, value: TraceMode) -> None:
        _validate_mode(value)
        with self._lock:
            self._mode = value

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, value: float) -> None:
        _validate_sample_rate(value)
        with self._lock:
            self._sample_rate = float(value)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def records(self) -> tuple[DispatchRecord, ...]:
        return self.snapshot()

    def configure(
        self,
        mode: TraceMode,
        *,
        sample_rate: float | None = None,
    ) -> bool:
        """Atomically configure tracing, returning whether it changed."""

        _validate_mode(mode)
        if sample_rate is not None:
            _validate_sample_rate(sample_rate)
        with self._lock:
            rate = (
                self._sample_rate
                if sample_rate is None
                else float(sample_rate)
            )
            if self._mode == mode and self._sample_rate == rate:
                return False
            self._mode = mode
            self._sample_rate = rate
            return True

    def enable(
        self,
        mode: Literal["sample", "all"] | None = None,
        *,
        sample_rate: float | None = None,
    ) -> bool:
        if mode is not None:
            _validate_enabled_mode(mode)
        if sample_rate is not None:
            _validate_sample_rate(sample_rate)
        with self._lock:
            target_mode = mode
            if target_mode is None:
                target_mode = self._mode if self._mode != "off" else "all"
            rate = (
                self._sample_rate
                if sample_rate is None
                else float(sample_rate)
            )
            if self._mode == target_mode and self._sample_rate == rate:
                return False
            self._mode = target_mode
            self._sample_rate = rate
            return True

    def disable(self) -> bool:
        with self._lock:
            if self._mode == "off":
                return False
            self._mode = "off"
            return True

    def record(self, record: DispatchRecord) -> bool:
        if not self.should_record(
            record.event_id,
            adapter_id=record.adapter_id,
            connection_id=record.connection_id,
            registry_version=record.registry_version,
            hook_id=record.hook_id,
        ):
            return False
        with self._lock:
            # Recheck under the lock so a concurrent mode change has a clear
            # boundary once it becomes visible to this thread.
            mode = self._mode
            if mode == "off":
                return False
            if mode == "sample" and not self._sampled(
                record,
                self._sample_rate,
            ):
                return False
            if len(self._records) == self._capacity:
                self._dropped += 1
            self._records.append(record)
        return True

    def should_record(
        self,
        event_id: str | None,
        *,
        adapter_id: str | None = None,
        connection_id: str | None = None,
        registry_version: int = 0,
        hook_id: int = 0,
    ) -> bool:
        """Check the current mode before a caller allocates a record."""

        mode = self._mode
        if mode == "off":
            return False
        if mode == "all":
            return True
        return self._sampled_key(
            event_id,
            adapter_id,
            connection_id,
            registry_version,
            hook_id,
            self._sample_rate,
        )

    def session_reference(self, session_id: str | None) -> str | None:
        """Return a recorder-local pseudonym suitable for correlation."""

        if session_id is None:
            return None
        digest = blake2s(
            str(session_id).encode("utf-8", "surrogatepass"),
            digest_size=12,
            key=self._redaction_key,
        ).hexdigest()
        return f"session:{digest}"

    @staticmethod
    def _sampled(record: DispatchRecord, sample_rate: float) -> bool:
        return TraceRecorder._sampled_key(
            record.event_id,
            record.adapter_id,
            record.connection_id,
            record.registry_version,
            record.hook_id,
            sample_rate,
        )

    @staticmethod
    def _sampled_key(
        event_id: str | None,
        adapter_id: str | None,
        connection_id: str | None,
        registry_version: int,
        hook_id: int,
        sample_rate: float,
    ) -> bool:
        if sample_rate <= 0.0:
            return False
        if sample_rate >= 1.0:
            return True
        # event_id is stable for every Hook in one dispatch, preserving a
        # complete event trace without counters or random-number state.
        key = event_id
        if key is None:
            key = (
                f"{adapter_id}:{connection_id}:"
                f"{registry_version}:{hook_id}"
            )
        bucket = zlib.crc32(str(key).encode("utf-8", "surrogatepass"))
        return bucket < int(sample_rate * (1 << 32))

    append = record

    def snapshot(self) -> tuple[DispatchRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def clear(self) -> int:
        with self._lock:
            count = len(self._records)
            self._records.clear()
            self._dropped = 0
        return count


# A shorter name is convenient for runtime configuration and remains explicit.
DispatchTrace = TraceRecorder


def _validate_mode(mode: object) -> None:
    if mode not in ("off", "sample", "all"):
        raise ValueError(f"unknown trace mode: {mode!r}")


def _validate_enabled_mode(mode: object) -> None:
    if mode not in ("sample", "all"):
        raise ValueError(f"enable mode must be 'sample' or 'all', got {mode!r}")


def _validate_sample_rate(sample_rate: object) -> None:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float)):
        raise TypeError("trace sample rate must be a number")
    if not 0.0 <= float(sample_rate) <= 1.0:
        raise ValueError("trace sample rate must be between 0 and 1")
