"""Explicit, startup-only extension composition. Disabled modules stay unloaded."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType

from core import LifecycleError, RuntimeState
from shared.extensions import ExtensionsConfig, REQUIRES, ordered_ids


@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    id: str
    requires: tuple[str, ...]
    module: str

    def validate(self, config):
        return getattr(config, self.id, None)

    def install(self, scope, config):
        return import_module(self.module).install(scope, config)


REGISTRY = MappingProxyType({name: ExtensionSpec(name, requires, f"hooks.extensions.{name}")
                           for name, requires in REQUIRES.items()})


@dataclass(frozen=True, slots=True)
class InstalledExtensions:
    scopes: object
    router: object = None


class ExtensionInstallationError(RuntimeError):
    def __init__(self, cause, failures, scopes):
        super().__init__(f"extension installation failed; rollback left {len(failures)} failures")
        self.cause, self.failures, self.scopes = cause, tuple(failures), tuple(scopes)


async def install_extensions(bot, config: ExtensionsConfig, *, registry=REGISTRY):
    if bot.runtime.state != RuntimeState.NEW:
        raise LifecycleError("extensions must be installed before runtime setup")
    order = ordered_ids(config.enabled, {name: spec.requires for name, spec in registry.items()})
    options = {name: registry[name].validate(config) for name in order}
    scopes = {name: bot.scope(f"business.{name}") for name in order}
    for name, scope in scopes.items():
        if (not scope.active or bot.dispatcher.handles_for_owner(scope.owner)
                or bot.providers.handles_for_owner(scope.owner) or bot.services.handles_for_owner(scope.owner)):
            raise ValueError(f"extension scope {name} has already been used; use a new Bot")
    keys = []
    try:
        for name in order:
            key = registry[name].install(scopes[name], options[name])
            if key is not None:
                keys.append(key)
        router = None
        if "commands" in scopes:
            from .commands import publish
            router = publish(scopes["commands"], config.commands.prefix, keys)
        bot.services.validate()
        return InstalledExtensions(MappingProxyType(scopes), router)
    except BaseException as cause:
        failures = []
        # The compiled router consumes all contributions; revoke it first.
        names = (["commands"] if "commands" in scopes else []) + [n for n in reversed(scopes) if n != "commands"]
        for name in names:
            try:
                await scopes[name].unload(mode="abort")
            except BaseException as error:
                failures.append(error)
        if failures:
            raise ExtensionInstallationError(cause, failures, scopes.values()) from cause
        raise


__all__ = ["ExtensionSpec", "InstalledExtensions", "ExtensionInstallationError", "install_extensions"]
