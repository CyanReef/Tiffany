"""Persistent directory layout, independent of Bot and server deployment."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    home: Path

    @classmethod
    def resolve(cls, home: str | Path | None = None) -> RuntimePaths:
        """Resolve the home directory without creating files."""
        from .path_setup import resolve_home

        return cls(resolve_home(home))

    def initialize(self) -> None:
        """Explicitly create the persistent layout and protect secret backups."""
        from .path_setup import initialize_layout

        initialize_layout(self)

    @property
    def config(self) -> Path:
        return self.home / "config" / "Tiffany.toml"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets"

    @property
    def credentials(self) -> Path:
        return self.secrets / "credentials.json"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def storage(self) -> Path:
        return self.home / "storage"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def envs(self) -> Path:
        return self.home / "envs"

    @property
    def run(self) -> Path:
        return self.home / "run"
