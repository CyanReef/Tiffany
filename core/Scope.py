from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .Field import Field
from .Hook import Handler, Hook, HookErrorPolicy, Predicate
from .Lifecycle import LifecycleError, RuntimeState
from .Service import ServiceKey


if TYPE_CHECKING:
    from .Bot import Bot
    from .Provider import ProviderHandle, ProviderContext
    from .Service import ServiceHandle


T = TypeVar("T")


class OwnerInUseError(RuntimeError):
    def __init__(self, owner: object, dependents: tuple[str, ...]) -> None:
        self.owner = owner
        self.dependents = dependents
        super().__init__(
            f"owner {owner!r} is in use by: {', '.join(dependents)}"
        )


class Scope:
    """Registration and lifecycle ownership boundary for one extension."""

    __slots__ = ("bot", "name", "owner", "_active")

    def __init__(self, bot: "Bot", name: str, owner: object | None = None) -> None:
        if not name:
            raise ValueError("scope name cannot be empty")
        self.bot = bot
        self.name = name
        self.owner = name if owner is None else owner
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def register_hook(
        self,
        handler: Handler,
        *,
        name: str | None = None,
        on: str | None = None,
        needs: tuple[Field[Any], ...] = (),
        priority: int = 0,
        when: Predicate | None = None,
        platform: str | None = None,
        on_error: HookErrorPolicy = "abort",
        timeout: float | None = None,
        on_timeout: HookErrorPolicy | None = None,
        uses: tuple[ServiceKey[Any], ...] = (),
        source: str | None = None,
    ):
        self._ensure_active()
        for key in uses:
            self.bot.services.get(key)
        source = source or _source_of(handler)
        handle = self.bot.dispatcher.add(Hook(
            name=name or getattr(handler, "__name__", type(handler).__name__),
            on=on,
            needs=tuple(needs),
            priority=priority,
            when=when,
            handle=handler,
            platform=platform,
            on_error=on_error,
            timeout=timeout,
            on_timeout=on_timeout,
            uses=tuple(uses),
            owner=self.owner,
            source=source,
        ))
        self.bot._validated_platforms.clear()
        return handle

    def hook(self, **options: Any):
        def decorator(func: Handler) -> Handler:
            self.register_hook(func, **options)
            return func

        return decorator

    def provide(
        self,
        field: Field[T],
        provider: Callable[["ProviderContext"], T],
        *,
        platform: str | None = None,
        requires: tuple[Field[Any], ...] = (),
        source: str | None = None,
    ) -> "ProviderHandle":
        self._ensure_active()
        handle = self.bot.providers.add(
            field,
            provider,
            platform=platform,
            requires=requires,
            owner=self.owner,
            namespace=self.name,
            source=source or _source_of(provider),
        )
        self.bot._validated_platforms.clear()
        return handle

    def service(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        dependencies: tuple[ServiceKey[Any], ...] = (),
        source: str | None = None,
    ) -> "ServiceHandle[T]":
        self._ensure_active()
        if self.bot.runtime.state != RuntimeState.NEW:
            raise LifecycleError("services must be registered before setup completes")
        handle = self.bot.services.register(
            key,
            service,
            dependencies=dependencies,
            owner=self.owner,
            namespace=self.name,
            source=source or _source_of(service),
        )
        self.bot._validated_platforms.clear()
        return handle

    def lifespan(self, context_manager: Any) -> Any:
        self._ensure_active()
        if self.bot.runtime.state != RuntimeState.NEW:
            raise LifecycleError("lifespans must be registered before setup")
        self.bot.runtime.add_lifespan(context_manager, owner=self.owner)
        self.bot._validated_platforms.clear()
        return context_manager

    def install(self, adapter: Any) -> Any:
        self._ensure_active()
        if self.bot.runtime.state != RuntimeState.NEW:
            raise LifecycleError("adapters must be installed before setup")
        return self.bot.install(adapter, owner=self.owner)

    async def unload(self, *, mode: str = "drain") -> None:
        await self.bot.unload(self.owner, mode=mode)
        self._active = False

    def _ensure_active(self) -> None:
        if not self._active:
            raise RuntimeError(f"scope {self.name!r} has been unloaded")


def _source_of(value: object) -> str | None:
    try:
        file = inspect.getsourcefile(value)
        line = inspect.getsourcelines(value)[1]
    except (OSError, TypeError):
        return None
    return f"{file}:{line}" if file is not None else None
