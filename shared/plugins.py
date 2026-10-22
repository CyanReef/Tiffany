"""Static local-plugin discovery; safe before application dependencies exist."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib

from .path_setup import PROJECT_ROOT

GROUP = "tiffany.plugins"
API_VERSION = 1
_ID = re.compile(r"[a-z][a-z0-9_-]*\Z")
_ENTRY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")


class PluginError(ValueError):
    """A terminal plugin preparation or startup error."""

    def __init__(self, plugin: str, phase: str, detail: str):
        self.plugin, self.phase = plugin, phase
        super().__init__(f"plugin {plugin} [{phase}]: {detail}; fix it or use plugins disable {plugin}")


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PluginError(str(path), "configuration", f"cannot read TOML ({type(error).__name__})") from error


@dataclass(frozen=True, slots=True)
class PluginSettings:
    paths: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()

    @classmethod
    def load(cls, paths):
        source = paths.plugin_settings
        if not source.exists():
            return cls()
        data = read_toml(source)
        if set(data) - {"paths", "disabled"}:
            raise PluginError("settings", "configuration", "unknown plugins.toml option")
        for key in ("paths", "disabled"):
            values = data.get(key, [])
            if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
                raise PluginError("settings", "configuration", f"{key} must be a list of nonempty strings")
        if any(not _ID.fullmatch(x) for x in data.get("disabled", [])):
            raise PluginError("settings", "configuration", "invalid disabled plugin ID")
        return cls(tuple(data.get("paths", [])), tuple(dict.fromkeys(data.get("disabled", []))))

    def encode(self) -> bytes:
        return ("paths = " + json.dumps(self.paths, ensure_ascii=False) + "\n"
                + "disabled = " + json.dumps(self.disabled, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class PluginSpec:
    id: str
    name: str
    version: str
    entry: str
    requires: tuple[str, ...]
    root: Path
    digest: str

    def record(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version,
                "entry": self.entry, "requires": list(self.requires),
                "root": str(self.root), "digest": self.digest}


def read_plugin(root: Path, *, required=False) -> PluginSpec | None:
    source = root / "pyproject.toml"
    if not source.is_file():
        if required:
            raise PluginError(str(root), "discovery", "source has no pyproject.toml")
        return None
    data = read_toml(source)
    project = data.get("project", {})
    try:
        entries = project.get("entry-points", {}).get(GROUP)
        if entries is None and not required:
            return None
        if not isinstance(entries, dict) or len(entries) != 1:
            raise ValueError("declare exactly one tiffany.plugins entry point")
        plugin_id, entry = next(iter(entries.items()))
        if not _ID.fullmatch(plugin_id) or not isinstance(entry, str) or not _ENTRY.fullmatch(entry):
            raise ValueError("invalid plugin ID or module:callable entry point")
        if plugin_id in ("plugins", "tiffany"):
            raise ValueError("plugin ID is reserved for application configuration")
        name, version = project.get("name"), project.get("version")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("project.name must be a package name")
        if canonical_name(name) in ("tiffany-hook-runtime", "pip", "setuptools", "wheel"):
            raise ValueError("plugin cannot replace the host or package tooling")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("project.version must be static")
        if set(project.get("dynamic", [])) & {"version", "dependencies", "entry-points"}:
            raise ValueError("version, dependencies and entry points must be static")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(x, str) or not x for x in dependencies):
            raise ValueError("project.dependencies must be a list of requirements")
        options = data.get("tool", {}).get("tiffany", {}).get("plugin", {})
        if not isinstance(options, dict) or set(options) - {"api", "requires"}:
            raise ValueError("invalid tool.tiffany.plugin options")
        if type(options.get("api")) is not int or options["api"] != API_VERSION:
            raise ValueError("supported tool.tiffany.plugin.api is 1")
        requires = options.get("requires", [])
        if not isinstance(requires, list) or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in requires):
            raise ValueError("requires must be a list of plugin IDs")
        if len(set(requires)) != len(requires):
            raise ValueError("duplicate plugin dependency")
    except (AttributeError, TypeError, ValueError) as error:
        raise PluginError(str(root), "declaration", str(error)) from error
    return PluginSpec(plugin_id, canonical_name(name), version, entry, tuple(requires),
                      root.resolve(), hashlib.sha256(source.read_bytes()).hexdigest())


@dataclass(frozen=True, slots=True)
class PluginPlan:
    specs: tuple[PluginSpec, ...]
    enabled: tuple[PluginSpec, ...]
    settings: PluginSettings

    def identity(self, root: Path = PROJECT_ROOT) -> dict:
        source = root / "pyproject.toml"
        lock = root / "requirements-server.lock"
        return {"api": API_VERSION, "host": str(root.resolve()),
                "host_digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                "lock_digest": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "plugins": [spec.record() for spec in self.enabled]}


def discover_plugins(paths, *, validate=True) -> PluginPlan:
    settings = PluginSettings.load(paths)
    candidates = [(p, False) for p in sorted(paths.plugins.iterdir()) if p.is_dir()] if paths.plugins.exists() else []
    candidates.extend(((paths.home / Path(p).expanduser()).resolve(), True) for p in settings.paths)
    specs, roots, names = {}, set(), set()
    for root, required in candidates:
        root = root.resolve()
        if root in roots:
            continue
        roots.add(root)
        spec = read_plugin(root, required=required)
        if spec is None:
            continue
        if spec.id in specs or spec.name in names:
            raise PluginError(spec.id, "discovery", "duplicate plugin ID or distribution name")
        specs[spec.id] = spec
        names.add(spec.name)
    enabled = {key: spec for key, spec in specs.items() if key not in settings.disabled}
    if not validate:
        return PluginPlan(tuple(specs.values()), tuple(enabled.values()), settings)
    order, visiting, visited = [], set(), set()

    def visit(key):
        if key in visiting:
            raise PluginError(key, "dependencies", "plugin dependency cycle")
        if key in visited:
            return
        if key not in enabled:
            raise PluginError(key, "dependencies", "required plugin is missing or disabled")
        visiting.add(key)
        for dependency in enabled[key].requires:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
        order.append(enabled[key])

    for key in sorted(enabled):
        visit(key)
    return PluginPlan(tuple(specs.values()), tuple(order), settings)
