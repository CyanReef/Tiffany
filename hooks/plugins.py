"""Startup-only external plugin registration with Scope-owned rollback."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import inspect

from core import RuntimeState
from shared.plugins import PluginError, PluginPlan, read_toml


@dataclass(frozen=True, slots=True)
class InstalledPlugins:
    scopes: tuple
    components: dict[str, tuple[str, ...]]

    def startup_failure(self, runtime) -> PluginError | None:
        if not runtime.startup_failed:
            return None
        for result in reversed(runtime.startup_results):
            if result.outcome != "completed" and result.component in self.components:
                return PluginError(",".join(self.components[result.component]), result.phase,
                                   f"{result.component} failed ({result.error_type}); see startup diagnostics")
        return None


async def install_plugins(bot, paths, plan: PluginPlan) -> InstalledPlugins:
    """Call synchronous install(scope, dict); on failure unload all new scopes."""
    if bot.runtime.state != RuntimeState.NEW:
        raise PluginError("application", "registration", "plugins must be installed before runtime setup")
    scopes = []
    current, phase = "application", "registration"
    try:
        for spec in plan.enabled:
            current, phase = spec.id, "configuration"
            source = paths.home / "config" / f"{spec.id}.toml"
            config = read_toml(source) if source.exists() else {}
            phase = "import"
            module, attributes = spec.entry.split(":", 1)
            install = import_module(module)
            for attribute in attributes.split("."):
                install = getattr(install, attribute)
            if not callable(install) or inspect.iscoroutinefunction(install):
                raise TypeError("entry must be synchronous install(scope, config) -> None")
            phase = "registration"
            scope = bot.scope(f"plugins.{spec.id}")
            if (not scope.active or bot.dispatcher.handles_for_owner(scope.owner)
                    or bot.providers.handles_for_owner(scope.owner) or bot.services.handles_for_owner(scope.owner)):
                raise ValueError("plugin scope already used; create a new Bot")
            scopes.append(scope)
            result = install(scope, config)
            if result is not None:
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("install(scope, config) must return None")
        bot.services.validate()
    except BaseException as cause:
        failures = []
        for scope in reversed(scopes):
            try:
                await scope.unload(mode="abort")
            except BaseException as error:
                failures.append(f"{scope.name}: {type(error).__name__}")
        if not isinstance(cause, Exception):
            raise
        detail = f"{type(cause).__name__}: {cause}"
        if failures:
            detail += "; rollback left scopes: " + ", ".join(failures)
        raise PluginError(current, phase, detail) from cause
    components = {}
    for scope in scopes:
        resources = [handle.component for handle in bot.services.handles_for_owner(scope.owner)]
        resources.extend(reg.manager for reg in bot.runtime._lifespans if reg.owner == scope.owner)
        resources.extend(adapter for adapter, owner in bot.runtime._adapter_owners if owner == scope.owner)
        resources.extend(adapter for adapter, owner in bot.runtime._adapter_owners if owner == scope.owner)
        for component in resources:
            name = getattr(component, "name", None) or type(component).__name__
            components.setdefault(name, []).append(scope.name.removeprefix("plugins."))
    return InstalledPlugins(tuple(scopes), {key: tuple(value) for key, value in components.items()})
