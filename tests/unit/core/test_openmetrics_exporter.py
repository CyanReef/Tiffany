"""OpenMetrics export with isolated optional dependencies."""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from core.Metrics import MetricRegistry
from core.OpenMetrics import (
    MissingOptionalDependencyError,
    OpenMetricsExporter,
)


class _Runtime:
    def __init__(self) -> None:
        self.metrics = MetricRegistry()


class _Metric:
    def __init__(self, name, documentation, typ) -> None:
        self.name = name
        self.documentation = documentation
        self.typ = typ
        self.samples = []

    def add_sample(self, name, labels, value) -> None:
        self.samples.append((name, labels, value))


class _Registry:
    def __init__(self, *, auto_describe) -> None:
        self.auto_describe = auto_describe
        self.collector = None

    def register(self, collector) -> None:
        self.collector = collector


class _Router:
    def __init__(self) -> None:
        self.path = None
        self.handler = None

    def add_get(self, path, handler) -> None:
        self.path = path
        self.handler = handler


class _Application:
    def __init__(self) -> None:
        self.router = _Router()


class _Response:
    def __init__(self, *, body, headers) -> None:
        self.body = body
        self.headers = headers


class _Runner:
    instances = []

    def __init__(self, app, *, access_log) -> None:
        self.app = app
        self.access_log = access_log
        self.setup_calls = 0
        self.cleanup_calls = 0
        type(self).instances.append(self)

    async def setup(self) -> None:
        self.setup_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


class _Site:
    instances = []

    def __init__(self, runner, *, host, port) -> None:
        self.runner = runner
        self.host = host
        self.port = port
        self.start_calls = 0
        type(self).instances.append(self)

    async def start(self) -> None:
        self.start_calls += 1


def _fake_import(name: str):
    if name == "aiohttp.web":
        return types.SimpleNamespace(
            Application=_Application,
            AppRunner=_Runner,
            TCPSite=_Site,
            Response=_Response,
        )
    if name == "prometheus_client":
        return types.SimpleNamespace(CollectorRegistry=_Registry)
    if name == "prometheus_client.core":
        return types.SimpleNamespace(Metric=_Metric)
    if name == "prometheus_client.openmetrics.exposition":
        def generate_latest(registry):
            metrics = tuple(registry.collector.collect())
            sample = metrics[0].samples[0]
            return f"{sample[0]} {sample[2]}\n# EOF\n".encode()

        return types.SimpleNamespace(
            generate_latest=generate_latest,
            CONTENT_TYPE_LATEST="application/openmetrics-text; version=1.0.0",
        )
    raise ModuleNotFoundError(name)


class OpenMetricsExporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _Runner.instances.clear()
        _Site.instances.clear()

    async def test_disabled_exporter_does_not_import_or_listen(self):
        exporter = OpenMetricsExporter()
        await exporter.setup(_Runtime())

        with patch(
            "core.OpenMetrics.importlib.import_module",
            side_effect=AssertionError("optional dependencies were imported"),
        ):
            await exporter.start()
            await exporter.start()
            await exporter.stop("drain")
            await exporter.teardown()

        self.assertFalse(exporter.running)
        self.assertEqual(_Runner.instances, [])

    async def test_enabled_exporter_requires_optional_dependencies(self):
        exporter = OpenMetricsExporter(enabled=True)
        await exporter.setup(_Runtime())

        with patch(
            "core.OpenMetrics.importlib.import_module",
            side_effect=ModuleNotFoundError("aiohttp"),
        ):
            with self.assertRaises(MissingOptionalDependencyError) as caught:
                await exporter.start()

        self.assertEqual(caught.exception.extra, "openmetrics")
        self.assertFalse(exporter.running)

    async def test_lifecycle_is_idempotent_and_scrapes_current_snapshot(self):
        runtime = _Runtime()
        runtime.metrics.inc("events_received_total", 3)
        exporter = OpenMetricsExporter(enabled=True, port=0)
        await exporter.setup(runtime)

        with patch(
            "core.OpenMetrics.importlib.import_module",
            side_effect=_fake_import,
        ):
            await exporter.start()
            await exporter.start()

        self.assertTrue(exporter.running)
        self.assertEqual(len(_Runner.instances), 1)
        self.assertEqual(len(_Site.instances), 1)
        self.assertEqual(_Site.instances[0].start_calls, 1)
        runner = _Runner.instances[0]
        self.assertEqual(runner.app.router.path, "/metrics")

        response = await runner.app.router.handler(object())
        self.assertEqual(response.body, b"events_received_total 3.0\n# EOF\n")
        self.assertIn("application/openmetrics-text", response.headers["Content-Type"])

        await exporter.stop("drain")
        await exporter.stop("abort")
        await exporter.teardown()
        self.assertFalse(exporter.running)
        self.assertEqual(runner.cleanup_calls, 1)

    def test_remote_binding_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow_remote"):
            OpenMetricsExporter(enabled=True, host="0.0.0.0")

        exporter = OpenMetricsExporter(
            enabled=True,
            host="0.0.0.0",
            allow_remote=True,
        )
        self.assertEqual(exporter.host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
