from __future__ import annotations

import asyncio
import importlib
import ipaddress
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .Lifecycle import StopMode
from .Metrics import MetricRegistry


class MissingOptionalDependencyError(RuntimeError):
    """Raised when an enabled optional extension cannot load its dependencies."""

    def __init__(self, extra: str, packages: tuple[str, ...]) -> None:
        package_list = ", ".join(packages)
        super().__init__(
            f"optional {extra!r} support requires {package_list}; "
            f"install Tiffany[{extra}]"
        )
        self.extra = extra
        self.packages = packages


@dataclass(frozen=True, slots=True)
class _Dependencies:
    web: ModuleType
    prometheus: ModuleType
    prometheus_core: ModuleType
    exposition: ModuleType


class _SnapshotCollector:
    """Translate an immutable core snapshot at scrape time."""

    __slots__ = ("_metrics", "_metric_type", "_scheduler")

    def __init__(self, metrics: MetricRegistry, metric_type: type[Any], scheduler=None) -> None:
        self._metrics = metrics
        self._metric_type = metric_type
        self._scheduler = scheduler

    def collect(self):
        grouped: dict[str, list[Any]] = {}
        for sample in self._metrics.snapshot().samples:
            grouped.setdefault(sample.name, []).append(sample)

        for name, samples in grouped.items():
            metric = self._metric_type(
                name,
                "Tiffany runtime metric.",
                "gauge",
            )
            for sample in samples:
                metric.add_sample(name, dict(sample.labels), sample.value)
            yield metric
        if self._scheduler is not None:
            for name, value in self._scheduler.snapshot().items():
                metric = self._metric_type(name, "Tiffany scheduler budget.", "gauge")
                metric.add_sample(name, {}, value)
                yield metric


class OpenMetricsExporter:
    """Optional HTTP exporter backed by runtime metric snapshots.

    Optional packages are imported only when an enabled exporter starts. The
    exporter owns a private collector registry so importing or running Tiffany
    never mutates prometheus-client's process-global registry.
    """

    __slots__ = (
        "enabled",
        "host",
        "port",
        "path",
        "allow_remote",
        "_runtime",
        "_lock",
        "_started",
        "_runner",
        "_site",
        "_dependencies",
        "_collector_registry",
        "_live",
        "_ready",
    )

    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "127.0.0.1",
        port: int = 9464,
        path: str = "/metrics",
        allow_remote: bool = False,
        live: Callable[[], Any] | None = None,
        ready: Callable[[], Any] | None = None,
    ) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("OpenMetrics port must be an integer from 0 to 65535")
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("OpenMetrics path must be an absolute URL path")
        if not allow_remote and not _is_loopback(host):
            raise ValueError(
                "non-loopback OpenMetrics binding requires allow_remote=True"
            )
        if path in ("/livez", "/readyz"):
            raise ValueError("OpenMetrics path conflicts with a health endpoint")

        self.enabled = enabled
        self.host = host
        self.port = port
        self.path = path
        self.allow_remote = allow_remote
        self._live = live
        self._ready = ready
        self._runtime: object | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._runner: Any = None
        self._site: Any = None
        self._dependencies: _Dependencies | None = None
        self._collector_registry: Any = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def setup(self, runtime: object) -> None:
        metrics = getattr(runtime, "metrics", None)
        if not isinstance(metrics, MetricRegistry):
            raise TypeError("runtime.metrics must be a MetricRegistry")
        if self._runtime is not None and self._runtime is not runtime:
            raise RuntimeError("OpenMetrics exporter is already attached to a runtime")
        self._runtime = runtime

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._runtime is None:
                raise RuntimeError("OpenMetrics exporter must be set up before start")
            if not self.enabled:
                self._started = True
                return

            dependencies = _load_optional_dependencies()
            metrics = getattr(self._runtime, "metrics")
            registry = dependencies.prometheus.CollectorRegistry(
                auto_describe=False
            )
            registry.register(
                _SnapshotCollector(metrics, dependencies.prometheus_core.Metric,
                                   getattr(self._runtime, "scheduler", None))
            )

            async def handle_metrics(request: Any) -> Any:
                del request
                payload = dependencies.exposition.generate_latest(registry)
                return dependencies.web.Response(
                    body=payload,
                    headers={
                        "Content-Type": dependencies.exposition.CONTENT_TYPE_LATEST
                    },
                )

            app = dependencies.web.Application()
            app.router.add_get(self.path, handle_metrics)
            def health_handler(callback):
                async def handle(request):
                    del request
                    try:
                        healthy = callback()
                        if inspect.isawaitable(healthy):
                            healthy = await healthy
                    except Exception:
                        healthy = False
                    return dependencies.web.Response(
                        body=b"ok\n" if healthy else b"unavailable\n",
                        status=200 if healthy else 503,
                        headers={"Content-Type": "text/plain; charset=utf-8"},
                    )
                return handle
            if self._live is not None:
                app.router.add_get("/livez", health_handler(self._live))
            if self._ready is not None:
                app.router.add_get("/readyz", health_handler(self._ready))
            runner = dependencies.web.AppRunner(app, access_log=None)
            await runner.setup()
            site = dependencies.web.TCPSite(
                runner,
                host=self.host,
                port=self.port,
            )
            try:
                await site.start()
            except BaseException:
                await runner.cleanup()
                raise

            self._dependencies = dependencies
            self._collector_registry = registry
            self._runner = runner
            self._site = site
            self._started = True

    async def stop(self, mode: StopMode = "drain") -> None:
        del mode
        async with self._lock:
            runner = self._runner
            self._runner = None
            self._site = None
            if runner is not None:
                await runner.cleanup()

    async def teardown(self) -> None:
        await self.stop("abort")


def _load_optional_dependencies() -> _Dependencies:
    try:
        web = importlib.import_module("aiohttp.web")
        prometheus = importlib.import_module("prometheus_client")
        prometheus_core = importlib.import_module("prometheus_client.core")
        exposition = importlib.import_module(
            "prometheus_client.openmetrics.exposition"
        )
    except (ImportError, ModuleNotFoundError) as error:
        raise MissingOptionalDependencyError(
            "openmetrics",
            ("aiohttp", "prometheus-client"),
        ) from error
    return _Dependencies(web, prometheus, prometheus_core, exposition)


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
