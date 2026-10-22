"""Standard-library configuration for explicitly selected local extensions."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType


REQUIRES = MappingProxyType({
    "commands": (), "ping": ("commands",), "help": ("commands",),
    "permissions": ("commands",), "ratelimit": ("commands",), "notes": ("commands",),
})


def ordered_ids(enabled, requirements=REQUIRES):
    if not isinstance(enabled, (list, tuple)) or any(not isinstance(x, str) for x in enabled):
        raise ValueError("extensions.enabled must be a list of extension IDs")
    if len(set(enabled)) != len(enabled):
        raise ValueError("duplicate extension ID")
    selected = set(enabled)
    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name not in requirements:
            raise ValueError(f"unknown extension ID: {name}")
        if name in visiting:
            raise ValueError(f"extension dependency cycle: {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in requirements[name]:
            if dependency not in selected:
                raise ValueError(f"extension {name} requires explicitly enabled {dependency}")
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in enabled:
        visit(name)
    return tuple(ordered)


def positive(value, name):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def integer(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class CommandsConfig:
    prefix: str = "/"

    def __post_init__(self):
        if not isinstance(self.prefix, str) or not self.prefix or any(c.isspace() for c in self.prefix):
            raise ValueError("extensions.commands.prefix must be nonempty without whitespace")


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    adapter_id: str
    scene: str
    user_id: int | str

    def __post_init__(self):
        if any(not isinstance(x, str) or not x for x in (self.adapter_id, self.scene)):
            raise ValueError("admin adapter_id and scene must be nonempty strings")
        if type(self.user_id) not in (int, str) or self.user_id == "":
            raise ValueError("admin user_id must retain its native int or nonempty string type")


@dataclass(frozen=True, slots=True)
class PermissionsConfig:
    admins: tuple[AdminIdentity, ...] = ()

    def __post_init__(self):
        if not isinstance(self.admins, (list, tuple)):
            raise ValueError("extensions.permissions.admins must be a list")
        try:
            entries = tuple(x if isinstance(x, AdminIdentity) else AdminIdentity(**x) for x in self.admins)
        except (TypeError, KeyError) as error:
            raise ValueError("invalid admin identity") from error
        object.__setattr__(self, "admins", entries)


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    rate: float = 1.0
    burst: int = 5
    max_buckets: int = 4096
    idle_ttl: float = 600.0

    def __post_init__(self):
        positive(self.rate, "ratelimit.rate")
        positive(self.idle_ttl, "ratelimit.idle_ttl")
        integer(self.burst, "ratelimit.burst", 1000000)
        integer(self.max_buckets, "ratelimit.max_buckets", 4096)
        if self.idle_ttl < self.burst / self.rate:
            raise ValueError("ratelimit.idle_ttl must cover a full bucket refill")


@dataclass(frozen=True, slots=True)
class NotesConfig:
    max_entries: int = 10000
    max_bytes: int = 4096
    max_inflight: int = 64

    def __post_init__(self):
        integer(self.max_entries, "notes.max_entries", 10000)
        integer(self.max_bytes, "notes.max_bytes", 4096)
        integer(self.max_inflight, "notes.max_inflight", 64)


@dataclass(frozen=True, slots=True)
class ExtensionsConfig:
    enabled: tuple[str, ...] = ()
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    ratelimit: RateLimitConfig = field(default_factory=RateLimitConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)

    def __post_init__(self):
        object.__setattr__(self, "enabled", ordered_ids(self.enabled))

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            raise ValueError("extensions must be a table")
        unknown = set(data) - {"enabled", *REQUIRES}
        if unknown:
            raise ValueError(f"unknown extension configuration: {sorted(unknown)}")
        enabled = ordered_ids(data.get("enabled", []))
        options = {}
        types = {"commands": CommandsConfig, "permissions": PermissionsConfig,
                 "ratelimit": RateLimitConfig, "notes": NotesConfig}
        for name in REQUIRES:
            values = data.get(name, {})
            if not isinstance(values, dict):
                raise ValueError(f"extensions.{name} must be a table")
            try:
                if name in types:
                    options[name] = types[name](**values)
                elif values:
                    raise ValueError(f"extensions.{name} has no configuration options")
            except TypeError as error:
                raise ValueError(f"invalid extensions.{name} options") from error
        return cls(enabled=enabled, **options)
