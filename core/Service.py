from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar, cast

from .Ownership import same_owner


T = TypeVar("T")
_UNSET = object()


@dataclass(frozen=True, slots=True, eq=False)
class ServiceKey(Generic[T]):
    """An identity-based, typed key for an explicitly registered service."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("service key name cannot be empty")

    def __repr__(self) -> str:
        return f"ServiceKey({self.name!r})"


@dataclass(frozen=True, slots=True)
class ServiceEntry:
    """An immutable service declaration stored in a registry snapshot."""

    service: object
    key: ServiceKey[Any]
    dependencies: tuple[ServiceKey[Any], ...]
    registration_id: int | None
    namespace: str
    owner: object
    source: str | None


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """A stable service lookup view which remains valid after mutations."""

    revision: int
    services: Mapping[ServiceKey[Any], ServiceEntry]
    registrations: Mapping[int, ServiceEntry]


class ServiceConflictError(ValueError):
    """Raised when two declarations target the same service key."""

    def __init__(self, existing: ServiceEntry, incoming: ServiceEntry) -> None:
        self.existing = existing
        self.incoming = incoming
        self.incumbent = existing
        self.contender = incoming
        self.key = incoming.key
        super().__init__(
            f"service conflict for {incoming.key.name!r}: "
            f"{_describe_service(existing)} conflicts with "
            f"{_describe_service(incoming)}"
        )


class ServiceHandle(Generic[T]):
    """Ownership-aware handle for one service registration."""

    __slots__ = ("_registry", "_entry")

    def __init__(self, registry: ServiceRegistry, entry: ServiceEntry) -> None:
        self._registry = registry
        self._entry = entry

    @property
    def id(self) -> int:
        registration_id = self._entry.registration_id
        if registration_id is None:  # pragma: no cover - internal invariant
            raise RuntimeError("unpublished service registration has no id")
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
    def key(self) -> ServiceKey[T]:
        return cast(ServiceKey[T], self._entry.key)

    @property
    def service(self) -> T:
        return cast(T, self._entry.service)

    @property
    def component(self) -> T:
        """Alias used by lifecycle orchestration."""

        return self.service

    @property
    def dependencies(self) -> tuple[ServiceKey[Any], ...]:
        return self._entry.dependencies

    @property
    def active(self) -> bool:
        return self._registry._contains_registration(self.id)

    def revoke(self) -> bool:
        """Remove this registration once; later calls return False."""

        return self._registry._revoke_id(self.id)

    def __repr__(self) -> str:
        state = "active" if self.active else "revoked"
        return (
            f"ServiceHandle(id={self.id}, key={self.key.name!r}, "
            f"namespace={self.namespace!r}, state={state!r})"
        )


class ServiceOwnershipError(ValueError):
    """One service instance cannot have independent lifecycle owners."""

    def __init__(self, existing: ServiceEntry, incoming: ServiceEntry) -> None:
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"service instance already belongs to {existing.owner!r}; "
            f"share ServiceKey {existing.key.name!r} using declared dependencies"
        )


class ServiceRegistry:
    """Explicit services with lock-free reads and copy-on-write mutations."""

    __slots__ = ("_snapshot", "_next_id", "_handles", "_lock", "_on_change")

    def __init__(self) -> None:
        self._snapshot = ServiceSnapshot(
            revision=0,
            services=MappingProxyType({}),
            registrations=MappingProxyType({}),
        )
        self._next_id = 1
        self._handles: dict[int, ServiceHandle[Any]] = {}
        self._lock = threading.RLock()
        self._on_change: Callable[[], None] | None = None

    @property
    def revision(self) -> int:
        return self._snapshot.revision

    def snapshot(self) -> ServiceSnapshot:
        """Return the current immutable lookup snapshot in O(1)."""

        return self._snapshot

    def register(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        dependencies: tuple[ServiceKey[Any], ...] = (),
        owner: object = "application",
        namespace: str = "application",
        source: str | None = None,
    ) -> ServiceHandle[T]:
        _validate_namespace(namespace)
        dependencies = tuple(dependencies)

        with self._lock:
            current = self._snapshot
            existing = current.services.get(key)
            candidate = ServiceEntry(
                service=service,
                key=key,
                dependencies=dependencies,
                registration_id=None,
                namespace=namespace,
                owner=owner,
                source=source,
            )
            if existing is not None:
                if existing.service is service and not same_owner(existing.owner, owner):
                    raise ServiceOwnershipError(existing, candidate)
                if _same_service_declaration(existing, candidate):
                    registration_id = existing.registration_id
                    if registration_id is None:  # pragma: no cover
                        raise RuntimeError("published service has no registration id")
                    return cast(ServiceHandle[T], self._handles[registration_id])
                raise ServiceConflictError(existing, candidate)

            for declaration in current.services.values():
                if declaration.service is service and not same_owner(declaration.owner, owner):
                    raise ServiceOwnershipError(declaration, candidate)

            registration_id = self._next_id
            self._next_id += 1
            entry = ServiceEntry(
                service=service,
                key=key,
                dependencies=dependencies,
                registration_id=registration_id,
                namespace=namespace,
                owner=owner,
                source=source,
            )
            handle = ServiceHandle[T](self, entry)
            services = dict(current.services)
            services[key] = entry
            registrations = dict(current.registrations)
            registrations[registration_id] = entry

            self._handles[registration_id] = handle
            self._snapshot = ServiceSnapshot(
                revision=current.revision + 1,
                services=MappingProxyType(services),
                registrations=MappingProxyType(registrations),
            )
            if self._on_change is not None:
                self._on_change()
            return handle

    def add(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        dependencies: tuple[ServiceKey[Any], ...] = (),
        owner: object = "application",
        namespace: str = "application",
        source: str | None = None,
    ) -> ServiceHandle[T]:
        return self.register(
            key,
            service,
            dependencies=dependencies,
            owner=owner,
            namespace=namespace,
            source=source,
        )

    def get(
        self,
        key: ServiceKey[T],
        snapshot: ServiceSnapshot | None = None,
    ) -> T:
        """Return a service in O(1), without factories or implicit I/O."""

        return cast(T, self.get_entry(key, snapshot).service)

    def get_entry(
        self,
        key: ServiceKey[Any],
        snapshot: ServiceSnapshot | None = None,
    ) -> ServiceEntry:
        view = self._snapshot if snapshot is None else snapshot
        entry = view.services.get(key)
        if entry is None:
            raise LookupError(f"no service registered for key {key.name!r}")
        return entry

    def validate(self, snapshot: ServiceSnapshot | None = None) -> None:
        """Check that all service dependencies exist and are acyclic."""

        self.lifecycle_order(snapshot)

    def lifecycle_order(
        self,
        snapshot: ServiceSnapshot | None = None,
    ) -> tuple[ServiceHandle[Any], ...]:
        """Return dependency-first lifecycle handles in stable order."""

        view = self._snapshot if snapshot is None else snapshot
        visited: set[ServiceKey[Any]] = set()
        path: list[ServiceKey[Any]] = []
        ordered_ids: list[int] = []

        def visit(key: ServiceKey[Any]) -> None:
            if key in visited:
                return
            if key in path:
                cycle = path[path.index(key):] + [key]
                names = " -> ".join(item.name for item in cycle)
                raise ValueError(f"service dependency cycle: {names}")

            entry = view.services.get(key)
            if entry is None:
                parent = path[-1].name if path else "registry"
                raise LookupError(
                    f"service {parent!r} requires unregistered service "
                    f"{key.name!r}"
                )
            path.append(key)
            for dependency in entry.dependencies:
                visit(dependency)
            path.pop()
            visited.add(key)
            if entry.registration_id is not None:
                ordered_ids.append(entry.registration_id)

        for entry in view.registrations.values():
            visit(entry.key)

        with self._lock:
            return tuple(
                self._handles.get(registration_id)
                or ServiceHandle(self, view.registrations[registration_id])
                for registration_id in ordered_ids
            )

    def registrations(
        self,
        *,
        owner: object = _UNSET,
        namespace: str | None = None,
    ) -> tuple[ServiceHandle[Any], ...]:
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

    def handles_for_owner(self, owner: object) -> tuple[ServiceHandle[Any], ...]:
        return self.registrations(owner=owner)

    def for_owner(self, owner: object) -> tuple[ServiceHandle[Any], ...]:
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

        services = dict(current.services)
        registrations = dict(current.registrations)
        for entry in entries:
            services.pop(entry.key, None)
            if entry.registration_id is not None:
                registrations.pop(entry.registration_id, None)
                self._handles.pop(entry.registration_id, None)

        self._snapshot = ServiceSnapshot(
            revision=current.revision + 1,
            services=MappingProxyType(services),
            registrations=MappingProxyType(registrations),
        )
        if self._on_change is not None:
            self._on_change()
        return len(entries)


def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("service namespace cannot be empty")


def _same_service_declaration(
    existing: ServiceEntry,
    incoming: ServiceEntry,
) -> bool:
    return (
        same_owner(existing.owner, incoming.owner)
        and existing.namespace == incoming.namespace
        and existing.source == incoming.source
        and existing.service is incoming.service
        and existing.dependencies == incoming.dependencies
    )


def _describe_service(entry: ServiceEntry) -> str:
    source = f", source={entry.source!r}" if entry.source is not None else ""
    return f"namespace={entry.namespace!r}, owner={entry.owner!r}{source}"
