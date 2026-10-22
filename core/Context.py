import inspect
from abc import get_cache_token
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .Envelope import Envelope
from .Field import Field

if TYPE_CHECKING:
    from .Provider import ProviderRegistry, ProviderSnapshot
    from .Service import ServiceKey, ServiceRegistry, ServiceSnapshot


T = TypeVar("T")
_MISSING = object()


# Cache exact built-ins only; subclasses and generators retain value-level
# inspection. Virtual Awaitable registration changes the ABC cache token.
_BUILTIN_TYPES = frozenset((str, int, float, bool, bytes, dict, list, tuple, set, frozenset, type(None)))
_BUILTIN_AWAITABLE = {}


def _is_awaitable(value):
    cls = type(value)
    entry = _BUILTIN_AWAITABLE.get(cls)
    token = get_cache_token()
    if entry is not None and entry[0] == token:
        return entry[1]
    result = inspect.isawaitable(value)
    if cls in _BUILTIN_TYPES:
        _BUILTIN_AWAITABLE[cls] = (token, result)
    return result


class _FieldResolver:
    """Resolve against shared event state without retaining the Context."""

    __slots__ = ()

    def resolve(self, field: Field[T]) -> T:
        cached = self._cache.get(field, _MISSING)
        if cached is not _MISSING:
            return cast(T, cached)
        if field in self._resolving:
            cycle = self._resolving[self._resolving.index(field):] + [field]
            names = " -> ".join(item.name for item in cycle)
            raise RuntimeError(f"provider dependency cycle: {names}")
        self._resolving.append(field)
        try:
            entry = self._providers.get(field, self.platform, self._provider_snapshot)
            view = self._provider_view
            if view is None:
                view = self._provider_view = _ProviderView(self)
            value = entry.provider(view)
            if _is_awaitable(value):
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


class _ProviderView(_FieldResolver):
    """Capability-limited facade passed to synchronous providers."""

    __slots__ = ("_envelope", "_providers", "_provider_snapshot", "_cache", "_resolving")

    def __init__(self, context: "Context") -> None:
        # Share the immutable inputs and mutable field state, rather than a
        # back-reference to Context. A retained provider view still works;
        # ordinary events no longer create a Context -> view -> Context cycle.
        self._envelope = context.envelope
        self._providers = context._providers
        self._provider_snapshot = context._provider_snapshot
        self._cache = context._cache
        self._resolving = context._resolving

    @property
    def _provider_view(self):
        return self

    @property
    def raw(self) -> dict[str, Any]:
        return self._envelope.raw

    @property
    def platform(self) -> str:
        return self._envelope.platform

    @property
    def kind(self) -> str:
        return self._envelope.kind

    @property
    def event_id(self) -> str:
        return self._envelope.event_id

    @property
    def adapter_id(self) -> str:
        return self._envelope.adapter_id

    @property
    def connection_id(self) -> str | None:
        return self._envelope.connection_id

    @property
    def session_id(self) -> str | None:
        return self._envelope.session_id


class Context(_FieldResolver):
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
        self._provider_view = None
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
