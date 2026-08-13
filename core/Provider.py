from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, TypeVar, runtime_checkable

from .Field import Field
from .Ownership import same_owner


T = TypeVar("T")
_UNSET = object()


@runtime_checkable
class ProviderContext(Protocol):
    """The synchronous, I/O-free view made available to providers."""

    @property
    def raw(self) -> dict[str, Any]: ...

    @property
    def platform(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def resolve(self, field: Field[T]) -> T: ...


Provider: TypeAlias = Callable[[ProviderContext], Any]


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """An immutable provider declaration stored in a registry snapshot."""

    provider: Provider
    requires: tuple[Field[Any], ...]
    field: Field[Any]
    platform: str | None
    registration_id: int | None
    namespace: str
    owner: object
    source: str | None


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """A stable provider lookup view which remains valid after mutations."""

    revision: int
    providers: Mapping[Field[Any], Mapping[str | None, ProviderEntry]]
    registrations: Mapping[int, ProviderEntry]


class ProviderConflictError(ValueError):
    """Raised when two declarations target the same resolution slot."""

    def __init__(self, existing: ProviderEntry, incoming: ProviderEntry) -> None:
        self.existing = existing
        self.incoming = incoming
        # Alternative terminology is useful to diagnostics consumers.
        self.incumbent = existing
        self.contender = incoming
        self.field = incoming.field
        self.platform = incoming.platform
        scope = incoming.platform or "all platforms"
        super().__init__(
            f"provider conflict for {incoming.field.name!r} on {scope}: "
            f"{_describe_provider(existing)} conflicts with "
            f"{_describe_provider(incoming)}"
        )


class ProviderHandle:
    """Ownership-aware handle for one provider registration."""

    __slots__ = ("_registry", "_entry")

    def __init__(self, registry: ProviderRegistry, entry: ProviderEntry) -> None:
        self._registry = registry
        self._entry = entry

    @property
    def id(self) -> int:
        registration_id = self._entry.registration_id
        if registration_id is None:  # pragma: no cover - internal invariant
            raise RuntimeError("unpublished provider registration has no id")
        return registration_id

    @property
    def namespace(self) -> str:
        return self._entry.namespace

    @property
    def owner(self) -> object:
        return self._entry.owner

    @property
    def source(self) -> str | None:
        return self._entry.source

    @property
    def field(self) -> Field[Any]:
        return self._entry.field

    @property
    def platform(self) -> str | None:
        return self._entry.platform

    @property
    def provider(self) -> Provider:
        return self._entry.provider

    @property
    def requires(self) -> tuple[Field[Any], ...]:
        return self._entry.requires

    @property
    def active(self) -> bool:
        return self._registry._contains_registration(self.id)

    def revoke(self) -> bool:
        """Remove this registration once; later calls return False."""

        return self._registry._revoke_id(self.id)

    def __repr__(self) -> str:
        state = "active" if self.active else "revoked"
        return (
            f"ProviderHandle(id={self.id}, field={self.field.name!r}, "
            f"platform={self.platform!r}, namespace={self.namespace!r}, "
            f"state={state!r})"
        )


class ProviderRegistry:
    """Synchronous providers with copy-on-write, revisioned snapshots."""

    __slots__ = ("_snapshot", "_next_id", "_handles", "_lock", "_on_change")

    def __init__(self) -> None:
        self._snapshot = ProviderSnapshot(
            revision=0,
            providers=MappingProxyType({}),
            registrations=MappingProxyType({}),
        )
        self._next_id = 1
        self._handles: dict[int, ProviderHandle] = {}
        self._lock = threading.RLock()
        self._on_change: Callable[[], None] | None = None

    @property
    def revision(self) -> int:
        return self._snapshot.revision

    def snapshot(self) -> ProviderSnapshot:
        """Return the current immutable lookup snapshot in O(1)."""

        return self._snapshot

    def add(
        self,
        field: Field[T],
        provider: Callable[[ProviderContext], T],
        *,
        platform: str | None = None,
        requires: tuple[Field[Any], ...] = (),
        owner: object = "application",
        namespace: str = "application",
        source: str | None = None,
    ) -> ProviderHandle:
        """Register a provider while preserving the legacy add call shape."""

        _validate_namespace(namespace)
        _validate_sync_provider(field, provider)
        requires = tuple(requires)

        with self._lock:
            current = self._snapshot
            scopes = current.providers.get(field)
            existing = scopes.get(platform) if scopes is not None else None
            candidate = ProviderEntry(
                provider=provider,
                requires=requires,
                field=field,
                platform=platform,
                registration_id=None,
                namespace=namespace,
                owner=owner,
                source=source,
            )

            if existing is not None:
                if _same_provider_declaration(existing, candidate):
                    registration_id = existing.registration_id
                    if registration_id is None:  # pragma: no cover
                        raise RuntimeError("published provider has no registration id")
                    return self._handles[registration_id]
                raise ProviderConflictError(existing, candidate)

            registration_id = self._next_id
            self._next_id += 1
            entry = ProviderEntry(
                provider=provider,
                requires=requires,
                field=field,
                platform=platform,
                registration_id=registration_id,
                namespace=namespace,
                owner=owner,
                source=source,
            )
            handle = ProviderHandle(self, entry)

            providers = dict(current.providers)
            updated_scopes = dict(scopes) if scopes is not None else {}
            updated_scopes[platform] = entry
            providers[field] = MappingProxyType(updated_scopes)
            registrations = dict(current.registrations)
            registrations[registration_id] = entry

            self._handles[registration_id] = handle
            self._snapshot = ProviderSnapshot(
                revision=current.revision + 1,
                providers=MappingProxyType(providers),
                registrations=MappingProxyType(registrations),
            )
            if self._on_change is not None:
                self._on_change()
            return handle

    def get(
        self,
        field: Field[Any],
        platform: str,
        snapshot: ProviderSnapshot | None = None,
    ) -> ProviderEntry:
        """Resolve a declaration from a supplied or current snapshot."""

        view = self._snapshot if snapshot is None else snapshot
        entries = view.providers.get(field)
        if entries is None:
            raise LookupError(f"no provider registered for field {field.name!r}")

        entry = entries.get(platform)
        if entry is None:
            entry = entries.get(None)
        if entry is None:
            raise LookupError(
                f"no provider registered for field {field.name!r} "
                f"on platform {platform!r}"
            )
        return entry

    def validate(
        self,
        fields: Iterable[Field[Any]],
        platform: str,
        snapshot: ProviderSnapshot | None = None,
    ) -> None:
        """Validate dependencies against one stable snapshot."""

        view = self._snapshot if snapshot is None else snapshot
        visited: set[Field[Any]] = set()
        path: list[Field[Any]] = []

        def visit(field: Field[Any]) -> None:
            if field in visited:
                return
            if field in path:
                cycle = path[path.index(field):] + [field]
                names = " -> ".join(item.name for item in cycle)
                raise ValueError(f"provider dependency cycle: {names}")

            path.append(field)
            entry = self.get(field, platform, view)
            for dependency in entry.requires:
                visit(dependency)
            path.pop()
            visited.add(field)

        for field in fields:
            visit(field)

    def registrations(
        self,
        *,
        owner: object = _UNSET,
        namespace: str | None = None,
    ) -> tuple[ProviderHandle, ...]:
        """Query active handles in registration order."""

        with self._lock:
            entries = self._snapshot.registrations.values()
            return tuple(
                self._handles[entry.registration_id]
                for entry in entries
                if entry.registration_id is not None
                and (owner is _UNSET or same_owner(entry.owner, owner))
                and (namespace is None or entry.namespace == namespace)
            )

    def handles_for_owner(self, owner: object) -> tuple[ProviderHandle, ...]:
        return self.registrations(owner=owner)

    def for_owner(self, owner: object) -> tuple[ProviderHandle, ...]:
        return self.handles_for_owner(owner)

    def revoke_owner(self, owner: object) -> int:
        """Atomically revoke all registrations owned by owner."""

        with self._lock:
            ids = tuple(
                registration_id
                for registration_id, entry in self._snapshot.registrations.items()
                if same_owner(entry.owner, owner)
            )
            return self._revoke_ids_locked(ids)

    def revoke_namespace(self, namespace: str) -> int:
        """Atomically revoke a diagnostic namespace."""

        with self._lock:
            ids = tuple(
                registration_id
                for registration_id, entry in self._snapshot.registrations.items()
                if entry.namespace == namespace
            )
            return self._revoke_ids_locked(ids)

    def _contains_registration(self, registration_id: int) -> bool:
        return registration_id in self._snapshot.registrations

    def _revoke_id(self, registration_id: int) -> bool:
        with self._lock:
            return bool(self._revoke_ids_locked((registration_id,)))

    def _revoke_ids_locked(self, registration_ids: Iterable[int]) -> int:
        current = self._snapshot
        unique_ids = dict.fromkeys(registration_ids)
        entries = tuple(
            current.registrations[registration_id]
            for registration_id in unique_ids
            if registration_id in current.registrations
        )
        if not entries:
            return 0

        providers = dict(current.providers)
        registrations = dict(current.registrations)
        for entry in entries:
            registration_id = entry.registration_id
            if registration_id is not None:
                registrations.pop(registration_id, None)
                self._handles.pop(registration_id, None)
            scopes = dict(providers[entry.field])
            scopes.pop(entry.platform, None)
            if scopes:
                providers[entry.field] = MappingProxyType(scopes)
            else:
                providers.pop(entry.field, None)

        self._snapshot = ProviderSnapshot(
            revision=current.revision + 1,
            providers=MappingProxyType(providers),
            registrations=MappingProxyType(registrations),
        )
        if self._on_change is not None:
            self._on_change()
        return len(entries)


def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("provider namespace cannot be empty")


def _validate_sync_provider(
    field: Field[Any],
    provider: Callable[[ProviderContext], Any],
) -> None:
    if not callable(provider):
        raise TypeError(f"provider for {field.name!r} must be callable")
    call = getattr(provider, "__call__", None)
    if inspect.iscoroutinefunction(provider) or inspect.iscoroutinefunction(call):
        raise TypeError(
            f"provider for {field.name!r} must be synchronous; "
            "perform I/O in a hook or service"
        )


def _same_provider_declaration(
    existing: ProviderEntry,
    incoming: ProviderEntry,
) -> bool:
    return (
        same_owner(existing.owner, incoming.owner)
        and existing.namespace == incoming.namespace
        and existing.source == incoming.source
        and existing.provider is incoming.provider
        and existing.requires == incoming.requires
    )


def _describe_provider(entry: ProviderEntry) -> str:
    source = f", source={entry.source!r}" if entry.source is not None else ""
    return (
        f"namespace={entry.namespace!r}, owner={entry.owner!r}{source}"
    )
