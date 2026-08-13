from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


MetricLabels = Mapping[str, str]
_ALLOWED_LABELS = frozenset({"platform", "adapter", "hook", "reason"})
_DEFAULT_MAX_SERIES = 4096


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

    __slots__ = ("_values", "_lock", "_max_series", "_dropped_series")

    def __init__(self, *, max_series: int = _DEFAULT_MAX_SERIES) -> None:
        if max_series < 1:
            raise ValueError("metric max_series must be at least 1")
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._lock = threading.Lock()
        self._max_series = max_series
        self._dropped_series = 0

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
        # Core intentionally avoids histogram machinery. Exporters can derive
        # averages from the paired sum/count series without affecting hot-path
        # allocations beyond two bounded dictionary updates.
        self.inc(f"{name}_count", labels=labels)
        self.inc(f"{name}_sum", value, labels=labels)

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
