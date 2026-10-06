"""Exact, bounded and immediately visible metric updates."""
import math
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import core.Metrics as metrics_module
from core import MetricRegistry


class MetricRegistryTests(unittest.TestCase):
    def test_bound_updates_match_public_api_without_preallocating_series(self):
        public, bound = MetricRegistry(), MetricRegistry()
        labels = {"platform": "test", "adapter": "a"}
        group = bound._bind(("count", "gauge", "peak"), labels)
        self.assertEqual(bound.snapshot().samples, ())
        public.inc("count", 2, labels=labels)
        public.set("gauge", 3, labels=labels)
        public.set_max("peak", 4, labels=labels)
        bound._batch((("inc", bound._bind(("count",), labels), (2,)),
                      ("set", bound._bind(("gauge",), labels), (3,)),
                      ("max", bound._bind(("peak",), labels), (4,))))
        self.assertEqual(public.snapshot().as_mapping(), bound.snapshot().as_mapping())
        self.assertEqual(len(group.keys), 3)

    def test_binding_reuses_validation_and_uses_weighted_lru(self):
        registry = MetricRegistry(max_series=3)
        with patch.object(metrics_module, "_normalize_labels", wraps=metrics_module._normalize_labels) as normalize:
            first = registry._bind(("a", "b"), {"adapter": "first"})
            registry._bind(("c",), {"adapter": "second"})
            self.assertIs(first, registry._bind(("a", "b"), {"adapter": "first"}))
            registry._bind(("d",), {"adapter": "third"})
            self.assertEqual(normalize.call_count, 3)
        self.assertEqual(registry._binding_size, 3)
        self.assertEqual(len(registry._bindings), 2)
        self.assertNotIn((("c",), (("adapter", "second"),)), registry._bindings)
        self.assertEqual(registry.snapshot().samples, ())

    def test_binding_rejects_unknown_labels_and_empty_groups(self):
        registry = MetricRegistry()
        with self.assertRaisesRegex(ValueError, "not allowed"):
            registry._bind(("count",), {"event_id": "unbounded"})
        with self.assertRaises(ValueError):
            registry._bind(())
        for method in (registry.inc, registry.set, registry.set_max, registry.observe):
            with self.assertRaises(ValueError):
                method("count", 1, labels={"session": "unbounded"})

    def test_admission_limit_and_dropped_attempts_match_public_updates(self):
        public, bound = MetricRegistry(max_series=1), MetricRegistry(max_series=1)
        public.observe("duration", .5)
        public.inc("new")
        group = bound._bind(("duration_count", "duration_sum", "new"))
        bound._batch((("inc", group, (1, .5, 1)),))
        self.assertEqual(public.snapshot().as_mapping(), bound.snapshot().as_mapping())
        self.assertEqual(public.dropped_series, bound.dropped_series)
        self.assertEqual(bound._binding_size, 0)

    def test_evicted_binding_remains_usable_and_does_not_bypass_series_limit(self):
        registry = MetricRegistry(max_series=1)
        old = registry._bind(("old",))
        new = registry._bind(("new",))
        registry._batch((("inc", old, (1,)), ("inc", new, (1,))))
        self.assertEqual(registry.snapshot().get("old"), 1)
        self.assertEqual(registry.snapshot().get("new"), 0)
        self.assertEqual(registry.dropped_series, 1)

    def test_invalid_batch_is_rejected_before_any_updates(self):
        registry = MetricRegistry()
        group = registry._bind(("count",))
        foreign = MetricRegistry()._bind(("count",))
        for invalid in (("inc", foreign, (1,)), ("invalid", group, (1,)), ("inc", group, ())):
            with self.assertRaises(ValueError):
                registry._batch((("inc", group, (1,)), invalid))
            self.assertEqual(registry.snapshot().samples, ())

    def test_maximum_nan_behavior_matches_public_api(self):
        public, bound = MetricRegistry(), MetricRegistry()
        group = bound._bind(("peak",))
        for value in (1, float("nan"), 2, -1):
            public.set_max("peak", value)
            bound._batch((("max", group, (value,)),))
            expected, actual = public.snapshot().get("peak"), bound.snapshot().get("peak")
            if math.isnan(expected):
                self.assertTrue(math.isnan(actual))
            else:
                self.assertEqual(expected, actual)

    def test_threaded_batches_and_snapshots_are_consistent(self):
        registry = MetricRegistry()
        def update():
            for _ in range(1000):
                group = registry._bind(("count", "sum"), {"adapter": "a"})
                registry._batch((("inc", group, (1, 1)),))
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(update) for _ in range(4)]
            while not all(job.done() for job in jobs):
                snapshot = registry.snapshot()
                self.assertEqual(snapshot.get("count", {"adapter": "a"}), snapshot.get("sum", {"adapter": "a"}))
            for job in jobs:
                job.result()
        self.assertEqual(registry.snapshot().get("count", {"adapter": "a"}), 4000)
        self.assertEqual(registry._binding_size, 2)


if __name__ == "__main__":
    unittest.main()
