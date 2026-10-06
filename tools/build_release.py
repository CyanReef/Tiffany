"""Build an overlay update archive from an explicit source allowlist."""
from __future__ import annotations

import argparse
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DIRECTORIES = ("core", "adapters", "clients", "hooks", "deployment", "shared", "examples", "docs", "tools")
FILES = ("start.sh", "start.bat", "start.ps1", "launcher.py", "main.py", "application.py", "settings.py", "fields.py",
         "pyproject.toml", "requirements.txt", "requirements-server.in", "requirements-server.lock",
         "MANIFEST.in", "README.md", "LICENSE")


def validate_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            parts = Path(name).parts
            if (len(parts) < 2 or parts[0] != "Tiffany" or ".." in parts
                    or parts[1].lower() == "data"):
                raise ValueError(f"unsafe release entry: {name}")
        if "Tiffany/start.sh" not in archive.namelist():
            raise ValueError("release is missing start.sh")


def build_release(root: Path, output: Path) -> Path:
    root = root.resolve()
    files = [root / name for name in FILES if (root / name).is_file()]
    for name in DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            raise ValueError(f"release cannot include symlink: {directory}")
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise ValueError(f"release cannot include symlink: {path}")
                if (path.is_file() and "__pycache__" not in path.parts
                        and path.suffix not in (".pyc", ".pyo")):
                    files.append(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_symlink():
                raise ValueError(f"release cannot include symlink: {path}")
            archive.write(path, "Tiffany/" + path.relative_to(root).as_posix())
    validate_archive(output)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    path = build_release(ROOT, args.output or ROOT / "dist" / f"Tiffany-server-{version}.zip")
    print(f"Release verified (no data/): {path}")


if __name__ == "__main__":
    main()
