"""Complete production source boundary for the representative benchmark."""
from pathlib import Path


def tiffany_sources(root: Path) -> list[Path]:
    paths = [
        path
        for name in ("core", "adapters", "clients", "deployment", "shared")
        for path in sorted((root / name).rglob("*.py"))
    ]
    return paths + [root / "fields.py", root / "settings.py"]
