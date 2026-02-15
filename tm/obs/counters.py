"""Thread-safe metric primitives and a shared registry."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, MutableMapping, Tuple

_LabelKey = Tuple[Tuple[str, str], ...]


def _canon_labels(labels: Mapping[str, str] | None) -> _LabelKey:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class _MetricBase:
    def __init__(self, name: str, *, help: str | None = None) -> None:  # noqa: A002 - mimic Prom-style arg
        self.name = name
        self.help = help or ""
        self._lock = threading.RLock()

    def _with_lock(self, fn):
        with self._lock:
            return fn()


class Counter(_MetricBase):
    def __init__(self, name: str, *, help: str | None = None) -> None:
        super().__init__(name, help=help)
        self._values: MutableMapping[_LabelKey, float] = defaultdict(float)

    def inc(self, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        lbl = _canon_labels(labels)

        def _inner() -> None:
            self._values[lbl] += float(value)

        self._with_lock(_inner)

    def samples(self) -> List[Tuple[_LabelKey, float]]:
        def _inner() -> List[Tuple[_LabelKey, float]]:
            return list(self._values.items())

        return self._with_lock(_inner)


class Gauge(_MetricBase):
    def __init__(self, name: str, *, help: str | None = None) -> None:
        super().__init__(name, help=help)
        self._values: MutableMapping[_LabelKey, float] = defaultdict(float)

    def set(self, value: float, labels: Mapping[str, str] | None = None) -> None:
        lbl = _canon_labels(labels)

        def _inner() -> None:
            self._values[lbl] = float(value)

        self._with_lock(_inner)

    def inc(self, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        lbl = _canon_labels(labels)

        def _inner() -> None:
            self._values[lbl] += float(value)

        self._with_lock(_inner)

    def samples(self) -> List[Tuple[_LabelKey, float]]:
        def _inner() -> List[Tuple[_LabelKey, float]]:
            return list(self._values.items())

        return self._with_lock(_inner)


@dataclass
class _HistogramBucket:
    le: float
    count: float


class Histogram(_MetricBase):
    def __init__(self, name: str, *, help: str | None = None, buckets: Iterable[float] | None = None) -> None:
        super().__init__(name, help=help)
        bounds = sorted(set(float(b) for b in (buckets or [0.1, 0.5, 1.0, 2.5, 5.0, 10.0])))
        if bounds[-1] != float("inf"):
            bounds.append(float("inf"))
        self._buckets = tuple(bounds)
        self._values: MutableMapping[_LabelKey, List[_HistogramBucket]] = {}

    def observe(self, value: float, labels: Mapping[str, str] | None = None) -> None:
        lbl = _canon_labels(labels)

        def _inner() -> None:
            buckets = self._values.setdefault(
                lbl,
                [_HistogramBucket(le=b, count=0.0) for b in self._buckets],
            )
            v = float(value)
            for bucket in buckets:
                if v <= bucket.le:
                    bucket.count += 1.0
            # also track sum/count for convenience
            self._values.setdefault(lbl + (("_sum", ""),), [])

        self._with_lock(_inner)

    def samples(self) -> Dict[_LabelKey, List[_HistogramBucket]]:
        def _inner() -> Dict[_LabelKey, List[_HistogramBucket]]:
            return {
                lbl: [_HistogramBucket(le=bucket.le, count=bucket.count) for bucket in buckets]
                for lbl, buckets in self._values.items()
                if not lbl or lbl[-1][0] != "_sum"
            }

        return self._with_lock(_inner)


class Registry:
    """Maintain metric instances by name."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}

    def get_counter(self, name: str, *, help: str | None = None) -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, help=help)
            return self._counters[name]

    def get_gauge(self, name: str, *, help: str | None = None) -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, help=help)
            return self._gauges[name]

    def get_histogram(
        self,
        name: str,
        *,
        help: str | None = None,
        buckets: Iterable[float] | None = None,
    ) -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, help=help, buckets=buckets)
            return self._histograms[name]

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        with self._lock:
            return {
                "counters": {name: counter.samples() for name, counter in self._counters.items()},
                "gauges": {name: gauge.samples() for name, gauge in self._gauges.items()},
                "histograms": {name: histogram.samples() for name, histogram in self._histograms.items()},
            }


class _MetricsProxy:
    """Thread-safe proxy around a metrics registry.

    Allows swapping the underlying registry without forcing callers to import
    a fresh module, which keeps tests isolated when they reset metrics.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registry: Registry = Registry()

    def use(self, registry: "Registry") -> None:
        if not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry instance")
        with self._lock:
            self._registry = registry

    def reset(self) -> None:
        self.use(Registry())

    def _current(self) -> "Registry":
        with self._lock:
            return self._registry

    def get_counter(self, name: str, *, help: str | None = None) -> Counter:
        return self._current().get_counter(name, help=help)

    def get_gauge(self, name: str, *, help: str | None = None) -> Gauge:
        return self._current().get_gauge(name, help=help)

    def get_histogram(
        self,
        name: str,
        *,
        help: str | None = None,
        buckets: Iterable[float] | None = None,
    ) -> Histogram:
        return self._current().get_histogram(name, help=help, buckets=buckets)

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return self._current().snapshot()


metrics = _MetricsProxy()


def _reset_for_tests() -> None:
    metrics.reset()


def counter(
    name: str,
    labels: Iterable[str] | None = None,
    *,
    registry: Registry | _MetricsProxy = metrics,
) -> Callable[..., None]:
    """Create a lightweight increment helper for a counter metric."""

    label_names = tuple(labels or ())

    def _inc(**kwargs: object) -> None:
        label_values: Dict[str, str] = {}
        for label in label_names:
            if label not in kwargs:
                raise ValueError(f"Missing label '{label}' for counter '{name}'")
            label_values[label] = str(kwargs[label])
        metric = registry.get_counter(name)
        metric.inc(labels=label_values if label_values else None)

    return _inc


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "metrics",
    "_reset_for_tests",
    "counter",
]
