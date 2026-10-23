"""Filesystem policy for the shared layout; importing this module does no I/O."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .paths import RuntimePaths


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_home(home: str | Path | None = None) -> Path:
    selected = home or os.environ.get("TIFFANY_HOME")
    if selected:
        return Path(selected).expanduser().resolve()
    from importlib.resources import files
    installed = files("shared").joinpath("_resources", "host.json").is_file()
    try:
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        source = project.get("project", {}).get("name") == "tiffany-hook-runtime"
    except (OSError, ValueError):
        source = False
    return ((PROJECT_ROOT if source and not installed else Path.cwd()) / "data").resolve()


def initialize_layout(paths: RuntimePaths) -> None:
    paths.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("config", "secrets", "logs", "storage", "cache", "backups", "envs", "run", "plugins"):
        (paths.home / name).mkdir(exist_ok=True, mode=0o700)
    if os.name == "posix":
        paths.secrets.chmod(0o700)
        paths.backups.chmod(0o700)
        for backup in paths.backups.glob("credentials.json.*.bak"):
            backup.chmod(0o600)
