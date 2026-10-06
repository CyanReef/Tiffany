from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MetricLabels = Mapping[str, str]
_ALLOWED_LABELS = frozenset({"platform", "adapter", "hook", "reason"})
_DEFAULT_MAX_SERIES = 4096
_MetricKey = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True, slots=True)
class _BoundMetrics:
    """Only immutable metric keys; binding does not allocate exported series."""

    registry: "MetricRegistry"
    keys: tuple[_MetricKey, ...]


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    samples: tuple[MetricSample, ...]

    def get(self, name: str, labels: MetricLabels | None = None) -> float:
        key_labels = _normalize_labels(labels)
        for sample in self.samples:
            if sample.name == name and sample.labels == key_labels:
                return sample.value
        return 0.0

    def as_mapping(self) -> Mapping[tuple[str, tuple[tuple[str, str], ...]], float]:
        return MappingProxyType({
            (sample.name, sample.labels): sample.value for sample in self.samples
        })


class MetricRegistry:
    """Small in-process metric registry with bounded label cardinality."""

    __slots__ = ("_values", "_lock", "_max_series", "_dropped_series",
                 "_bindings", "_binding_size")

    def __init__(self, *, max_series: int = _DEFAULT_MAX_SERIES) -> None:
        if max_series < 1:
            raise ValueError("metric max_series must be at least 1")
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._lock = threading.Lock()
        self._max_series = max_series
        self._dropped_series = 0
        self._bindings: OrderedDict[tuple, _BoundMetrics] = OrderedDict()
        self._binding_size = 0

    def _bind(self, names: tuple[str, ...], labels: MetricLabels | None = None) -> _BoundMetrics:
        """Bind a stable internal label group once, with a weighted LRU bound."""
        if not names:
            raise ValueError("metric binding must contain at least one name")
        cache_key = (names, tuple(labels.items()) if labels else ())
        with self._lock:
            bound = self._bindings.get(cache_key)
            if bound is not None:
                self._bindings.move_to_end(cache_key)
                return bound
            normalized = _normalize_labels(labels)
            bound = _BoundMetrics(self, tuple((name, normalized) for name in names))
            size = len(names)
            if size <= self._max_series:
                while self._binding_size + size > self._max_series:
                    _, evicted = self._bindings.popitem(last=False)
                    self._binding_size -= len(evicted.keys)
                self._bindings[cache_key] = bound
                self._binding_size += size
            return bound

    def _batch(self, updates: tuple[tuple[str, _BoundMetrics, tuple[float, ...]], ...]) -> None:
        """Commit one state transition under the same lock used by snapshots."""
        for operation, bound, values in updates:
            if bound.registry is not self:
                raise ValueError("metric binding belongs to another registry")
            if operation not in ("inc", "set", "max") or len(values) != len(bound.keys):
                raise ValueError("invalid metric batch")
        with self._lock:
            for operation, bound, values in updates:
                for key, value in zip(bound.keys, values):
                    current = self._values.get(key)
                    if current is None and len(self._values) >= self._max_series:
                        self._dropped_series += 1
                        continue
                    if operation == "inc":
                        self._values[key] = (current or 0.0) + value
                    elif operation == "set":
                        if current != value:
                            self._values[key] = float(value)
                    elif current is None or not current >= value:
                        self._values[key] = float(value)

    @property
    def max_series(self) -> int:
        return self._max_series

    @property
    def dropped_series(self) -> int:
        with self._lock:
            return self._dropped_series

    def inc(
        self,
        name: str,
        amount: float = 1.0,
        *,
        labels: MetricLabels | None = None,
    ) -> None:
        key = (name, _normalize_labels(labels))
        with self._lock:
            if key not in self._values and len(self._values) >= self._max_series:
                self._dropped_series += 1
                return
            self._values[key] = self._values.get(key, 0.0) + amount

    def set(
        self,
        name: str,
        value: float,
        *,
        labels: MetricLabels | None = None,
    ) -> None:
        key = (name, _normalize_labels(labels))
        with self._lock:
            if key not in self._values and len(self._values) >= self._max_series:
                self._dropped_series += 1
                return
            self._values[key] = float(value)

    def set_max(
        self,
        name: str,
        value: float,
        *,
        labels: MetricLabels | None = None,
    ) -> None:
        key = (name, _normalize_labels(labels))
        with self._lock:
            current = self._values.get(key)
            if current is not None and current >= value:
                return
            if current is None and len(self._values) >= self._max_series:
                self._dropped_series += 1
                return
            self._values[key] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: MetricLabels | None = None,
    ) -> None:
        labels_key = _normalize_labels(labels)
        with self._lock:
            for key, amount in (((f"{name}_count", labels_key), 1.0),
                                ((f"{name}_sum", labels_key), value)):
                if key not in self._values and len(self._values) >= self._max_series:
                    self._dropped_series += 1
                    continue
                self._values[key] = self._values.get(key, 0.0) + amount

    def snapshot(self) -> MetricSnapshot:
        with self._lock:
            samples = tuple(
                MetricSample(name, labels, value)
                for (name, labels), value in self._values.items()
            )
        return MetricSnapshot(samples)


# Keep both spellings available for integrations that describe the component
# rather than one concrete registry instance.
MetricsRegistry = MetricRegistry


def _normalize_labels(labels: MetricLabels | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    unknown = labels.keys() - _ALLOWED_LABELS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"metric labels are not allowed: {names}")
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))
