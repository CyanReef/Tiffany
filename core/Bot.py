from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from .Context import Context
from .Dispatcher import Dispatcher
from .Envelope import Envelope
from .Field import Field
from .Hook import Handler, HookErrorPolicy, Predicate
from .Lifecycle import RuntimeState, StopMode
from .Ownership import OwnerKey, same_owner
from .Provider import ProviderContext, ProviderHandle, ProviderRegistry
from .Runtime import Runtime
from .Scope import OwnerInUseError, Scope
from .Service import ServiceHandle, ServiceKey, ServiceRegistry


T = TypeVar("T")


class Bot:
    """Stable developer facade for the hook runtime."""

    __slots__ = (
        "dispatcher",
        "providers",
        "services",
        "runtime",
        "application",
        "_scopes",
        "_validated_platforms",
    )

    def __init__(self) -> None:
        self.dispatcher = Dispatcher()
        self.providers = ProviderRegistry()
        self.services = ServiceRegistry()
        self.runtime = Runtime(self)
        self.dispatcher.metrics = self.runtime.metrics
        self.application = Scope(self, "application", "application")
        self._scopes: dict[str, Scope] = {"application": self.application}
        self._validated_platforms: dict[str, tuple[int, int, int]] = {}
        self.dispatcher._on_change = self._validated_platforms.clear
        self.providers._on_change = self._validated_platforms.clear
        self.services._on_change = self._validated_platforms.clear

    @property
    def metrics(self):
        return self.runtime.metrics

    def scope(self, name: str) -> Scope:
        existing = self._scopes.get(name)
        if existing is not None:
            return existing
        if self.runtime.state not in (
            RuntimeState.NEW,
            RuntimeState.SETTING_UP,
            RuntimeState.SETUP,
            RuntimeState.RUNNING,
        ):
            raise RuntimeError(
                f"cannot create a scope while runtime is {self.runtime.state.value!r}"
            )
        scope = Scope(self, name)
        self._scopes[name] = scope
        return scope

    def provide(
        self,
        field: Field[T],
        provider: Callable[[ProviderContext], T],
        *,
        platform: str | None = None,
        requires: tuple[Field[Any], ...] = (),
    ) -> ProviderHandle:
        return self.application.provide(
            field,
            provider,
            platform=platform,
            requires=requires,
        )

    def service(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        dependencies: tuple[ServiceKey[Any], ...] = (),
    ) -> ServiceHandle[T]:
        return self.application.service(
            key,
            service,
            dependencies=dependencies,
        )

    def hook(
        self,
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
    ):
        return self.application.hook(
            name=name,
            on=on,
            needs=needs,
            priority=priority,
            when=when,
            platform=platform,
            on_error=on_error,
            timeout=timeout,
            on_timeout=on_timeout,
            uses=uses,
        )

    def register_hook(self, handler: Handler, **options: Any):
        return self.application.register_hook(handler, **options)

    def handle_for(self, func: Handler):
        return self.dispatcher.handle_for(func)

    def install(self, adapter: Any, *, owner: object = "application") -> Any:
        installed = self.runtime.install(adapter, owner=owner)
        self._validated_platforms.clear()
        return installed

    def lifespan(self, context_manager: Any) -> Any:
        self.runtime.add_lifespan(context_manager, owner="application")
        self._validated_platforms.clear()
        return context_manager

    def _validation_token(self) -> tuple[int, int, int]:
        return (
            self.dispatcher.revision,
            self.providers.revision,
            self.services.revision,
        )

    def _is_validated(self, platform: str) -> bool:
        return self._validated_platforms.get(platform) == self._validation_token()

    def validate(self, platform: str) -> None:
        self.providers.validate(self.dispatcher.required_fields(platform), platform)
        for key in self.dispatcher.required_services(platform):
            self.services.get(key)
        self._validated_platforms[platform] = self._validation_token()

    def _validate_registrations(self) -> None:
        platforms = self.dispatcher.platforms()
        platforms.update(
            getattr(adapter, "platform")
            for adapter in self.runtime.adapters
            if getattr(adapter, "platform", None) is not None
        )
        for platform in platforms:
            self.validate(platform)
        # Platform-agnostic Hook service dependencies must also be validated
        # when no Adapter or platform-specific Hook is installed yet.
        for key in self.dispatcher.required_services(""):
            self.services.get(key)

    async def emit(self, envelope: Envelope) -> Context:
        result = await self.runtime.emit(envelope, reject=True, wait=True)
        assert isinstance(result, Context)
        return result

    async def emit_from_adapter(self, envelope: Envelope) -> bool:
        return await self.runtime.emit(envelope, reject=False, wait=False) is not None

    async def setup(self, runtime: object | None = None) -> None:
        self.runtime._caller_task = asyncio.current_task()
        await self.runtime.setup(runtime)

    async def start(self) -> None:
        await self.runtime.start()

    async def stop(self, mode: StopMode = "drain"):
        try:
            return await self.runtime.stop(mode)
        finally:
            self.runtime._caller_task = None

    async def teardown(self):
        try:
            return await self.runtime.teardown()
        finally:
            self.runtime._caller_task = None

    async def run_async(self) -> None:
        async with self.runtime:
            await asyncio.Event().wait()

    async def unload(self, owner: object, *, mode: str = "drain") -> None:
        if mode not in ("drain", "abort"):
            raise ValueError("unload mode must be 'drain' or 'abort'")
        dependents = self._owner_dependents(owner)
        if dependents:
            raise OwnerInUseError(owner, dependents)

        hook_handles = self.dispatcher.handles_for_owner(owner)
        hook_states = tuple((handle, handle.enabled) for handle in hook_handles)
        for handle in hook_handles:
            handle.disable()

        if self.runtime.state == RuntimeState.RUNNING:
            try:
                drained = await self.runtime.wait_owner_events(
                    owner,
                    timeout=30.0,
                )
                if drained:
                    drained = await self.runtime.tasks.wait_owner(
                        owner,
                        timeout=30.0,
                    )
                if not drained:
                    await self.runtime.scheduler.cancel_owner(owner, grace=1.0)
                    await self.runtime.tasks.cancel_owner(owner, grace=1.0)
            except BaseException:
                for handle, was_enabled in hook_states:
                    if was_enabled:
                        handle.enable()
                raise

        from .Lifecycle import ShutdownIncompleteError, ShutdownReport

        report = ShutdownReport(forced=mode == "abort")
        deadline = asyncio.get_running_loop().time() + 30.0

        adapters = self.runtime.adapters_for_owner(owner)
        for adapter in reversed(adapters):
            if any(item is adapter for item in self.runtime._started_adapters):
                stopped = await self.runtime._cleanup(
                    adapter, "stop", mode, report, deadline
                )
                if stopped:
                    _remove_identity(self.runtime._started_adapters, adapter)
                    self.runtime._started_adapter_ids.discard(id(adapter))
            if any(item is adapter for item in self.runtime._setup_components):
                torn_down = await self.runtime._cleanup(
                    adapter, "teardown", None, report, deadline
                )
                if torn_down:
                    _remove_identity(self.runtime._setup_components, adapter)
                    self.runtime._setup_ids.discard(id(adapter))

        lifespans = self.runtime.lifespans_for_owner(owner)
        for registration in reversed(lifespans):
            if any(
                item is registration
                for item in self.runtime._entered_lifespans
            ):
                exited = await self.runtime._cleanup(
                    registration.manager,
                    "__aexit__",
                    None,
                    report,
                    deadline,
                )
                if exited:
                    _remove_identity(
                        self.runtime._entered_lifespans,
                        registration,
                    )

        services = self.services.handles_for_owner(owner)
        ordered = tuple(
            handle
            for handle in self.services.lifecycle_order()
            if any(handle is owned for owned in services)
        )
        for handle in reversed(ordered):
            component = handle.component
            stopped = not any(
                item is component for item in self.runtime._started_services
            )
            if any(item is component for item in self.runtime._started_services):
                stopped = await self.runtime._cleanup(
                    component, "stop", mode, report, deadline
                )
                if stopped:
                    _remove_identity(self.runtime._started_services, component)
                    self.runtime._started_service_ids.discard(id(component))
            torn_down = not any(
                item is component for item in self.runtime._setup_components
            )
            if any(item is component for item in self.runtime._setup_components):
                torn_down = await self.runtime._cleanup(
                    component, "teardown", None, report, deadline
                )
                if torn_down:
                    _remove_identity(self.runtime._setup_components, component)
                    self.runtime._setup_ids.discard(id(component))
            if stopped and torn_down:
                _remove_identity(self.runtime._service_order, component)

        for handle in hook_handles:
            handle.remove()
        self.providers.revoke_owner(owner)
        self.services.revoke_owner(owner)
        if not report.failures and not report.abandoned_tasks:
            self.runtime._adapters[:] = [
                adapter
                for adapter in self.runtime._adapters
                if not any(adapter is owned for owned in adapters)
            ]
            self.runtime._adapter_owners[:] = [
                item
                for item in self.runtime._adapter_owners
                if not same_owner(item[1], owner)
            ]
            self.runtime._lifespans[:] = [
                registration
                for registration in self.runtime._lifespans
                if not same_owner(registration.owner, owner)
            ]
        scope = next((scope for scope in self._scopes.values()
                      if same_owner(scope.owner, owner)), None)
        if scope is not None:
            scope._active = False

        residue = (
            self.dispatcher.handles_for_owner(owner)
            or self.providers.handles_for_owner(owner)
            or self.services.handles_for_owner(owner)
            or self.runtime.tasks.tasks_for(owner)
            or self.runtime.adapters_for_owner(owner)
            or self.runtime.lifespans_for_owner(owner)
            or self.runtime._owner_events.get(OwnerKey(owner), 0)
        )
        if residue:
            report.failures.append(
                RuntimeError(f"owner {owner!r} left runtime residue")
            )
        if report.failures or report.abandoned_tasks:
            raise ShutdownIncompleteError(report)

    def _owner_dependents(self, owner: object) -> tuple[str, ...]:
        owned_fields = {
            handle.field for handle in self.providers.handles_for_owner(owner)
        }
        owned_services = {
            handle.key for handle in self.services.handles_for_owner(owner)
        }
        dependencies: list[str] = []
        for handle in self.providers.registrations():
            if not same_owner(handle.owner, owner) and owned_fields.intersection(handle.requires):
                dependencies.append(f"provider:{handle.namespace}")
        for handle in self.services.registrations():
            if not same_owner(handle.owner, owner) and owned_services.intersection(handle.dependencies):
                dependencies.append(f"service:{handle.namespace}")
        for handle in self.dispatcher.handles():
            hook = handle.hook
            if not same_owner(hook.owner, owner) and (
                owned_fields.intersection(hook.needs)
                or owned_services.intersection(hook.uses)
            ):
                dependencies.append(f"hook:{hook.name}")
        return tuple(dict.fromkeys(dependencies))

    async def __aenter__(self) -> "Bot":
        await self.runtime.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return await self.runtime.__aexit__(exc_type, exc, tb)


def _remove_identity(items: list[Any], value: object) -> bool:
    for index, item in enumerate(items):
        if item is value:
            del items[index]
            return True
    return False
