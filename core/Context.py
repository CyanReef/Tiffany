import inspect
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .Envelope import Envelope
from .Field import Field

if TYPE_CHECKING:
    from .Provider import ProviderRegistry, ProviderSnapshot
    from .Service import ServiceKey, ServiceRegistry, ServiceSnapshot


T = TypeVar("T")
_MISSING = object()


class _ProviderView:
    """Capability-limited facade passed to synchronous providers."""

    __slots__ = ("_context",)

    def __init__(self, context: "Context") -> None:
        self._context = context

    @property
    def raw(self) -> dict[str, Any]:
        return self._context.raw

    @property
    def platform(self) -> str:
        return self._context.platform

    @property
    def kind(self) -> str:
        return self._context.kind

    @property
    def event_id(self) -> str:
        return self._context.envelope.event_id

    @property
    def adapter_id(self) -> str:
        return self._context.envelope.adapter_id

    @property
    def connection_id(self) -> str | None:
        return self._context.envelope.connection_id

    @property
    def session_id(self) -> str | None:
        return self._context.envelope.session_id

    def resolve(self, field: Field[T]) -> T:
        return self._context.resolve(field)


class Context:
    """A raw-first event view with shared, per-event lazy field caching."""

    __slots__ = (
        "envelope",
        "_providers",
        "_provider_snapshot",
        "_services",
        "_service_snapshot",
        "_provider_view",
        "_cache",
        "_resolving",
        "stopped",
    )

    def __init__(
        self,
        envelope: Envelope,
        providers: "ProviderRegistry",
        services: "ServiceRegistry | None" = None,
        *,
        provider_snapshot: "ProviderSnapshot | None" = None,
        service_snapshot: "ServiceSnapshot | None" = None,
    ) -> None:
        self.envelope = envelope
        self._providers = providers
        self._provider_snapshot = provider_snapshot or providers.snapshot()
        self._services = services
        self._service_snapshot = (
            service_snapshot
            if service_snapshot is not None
            else services.snapshot() if services is not None else None
        )
        self._provider_view = _ProviderView(self)
        self._cache: dict[Field[Any], Any] = {}
        self._resolving: list[Field[Any]] = []
        self.stopped = False

    @property
    def raw(self) -> dict[str, Any]:
        return self.envelope.raw

    @property
    def platform(self) -> str:
        return self.envelope.platform

    @property
    def kind(self) -> str:
        return self.envelope.kind

    @property
    def client(self) -> Any | None:
        return self.envelope.client

    def stop(self) -> None:
        self.stopped = True

    def has(self, field: Field[Any]) -> bool:
        return field in self._cache

    def put(self, field: Field[T], value: T) -> T:
        """Publish an already computed value for later hooks and providers."""

        self._cache[field] = value
        return value

    def resolve(self, field: Field[T]) -> T:
        """Resolve a synchronous provider graph once for this event."""

        cached = self._cache.get(field, _MISSING)
        if cached is not _MISSING:
            return cast(T, cached)

        if field in self._resolving:
            cycle = self._resolving[self._resolving.index(field):] + [field]
            names = " -> ".join(item.name for item in cycle)
            raise RuntimeError(f"provider dependency cycle: {names}")

        self._resolving.append(field)
        try:
            entry = self._providers.get(
                field,
                self.platform,
                self._provider_snapshot,
            )
            value = entry.provider(self._provider_view)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError(
                    f"provider for {field.name!r} returned an awaitable; "
                    "providers must be synchronous"
                )

            self._cache[field] = value
            return cast(T, value)
        finally:
            self._resolving.pop()

    def service(self, key: "ServiceKey[T]") -> T:
        if self._services is None or self._service_snapshot is None:
            raise LookupError(f"no service registry is available for {key.name!r}")
        return self._services.get(key, self._service_snapshot)

    async def reply(self, text: str) -> Any:
        if self.client is None:
            raise RuntimeError("current context has no client, cannot reply")
        reply = getattr(self.client, "reply", None)
        if reply is not None:
            return await reply(self, text)
        return await self.client.reply_text(self, text)
