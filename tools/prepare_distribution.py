"""Build release artifacts once and publish a SHA-256 inventory, without uploading."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def validate(path):
    if path.suffix in (".zip", ".whl"):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(path) as archive:
            names = archive.getnames()
    for name in names:
        parts = Path(name).parts
        if any(p in ("data", ".build-cache", ".venv", "__pycache__") for p in parts):
            raise ValueError(f"runtime content in release: {name}")
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for resource in ("shared/_resources/host.json", "shared/_resources/requirements-server.lock"):
                if resource not in names:
                    raise ValueError(f"missing runtime resource: {resource}")
            if not any(".dist-info/licenses/" in n for n in names):
                raise ValueError("wheel license is missing")
            manifest = json.loads(archive.read("shared/_resources/host.json"))
            allowed = set(manifest["files"]) | {"shared/_resources/host.json"}
            if any(n not in allowed and ".dist-info/" not in n for n in names if not n.endswith("/")):
                raise ValueError("wheel contains runtime files outside its host manifest")
            for name, checksum in manifest["files"].items():
                if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                    raise ValueError(f"wheel manifest mismatch: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    parser.add_argument("--no-isolation", action="store_true", help="use preinstalled PEP 517 build requirements")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tiffany-release-") as temporary:
        staging = Path(temporary)
        command = [sys.executable, "-B", "-m", "build", "--outdir", str(staging), str(ROOT)]
        if args.no_isolation:
            command.append("--no-isolation")
        subprocess.run(command, check=True)
        # The default build rebuilds its wheel from sdist. Compare a direct build.
        with tempfile.TemporaryDirectory(prefix="tiffany-direct-wheel-") as direct:
            direct_command = [sys.executable, "-B", "-m", "build", "--wheel", "--outdir", direct, str(ROOT)]
            if args.no_isolation:
                direct_command.append("--no-isolation")
            subprocess.run(direct_command, check=True)
            with zipfile.ZipFile(next(staging.glob("*.whl"))) as rebuilt, \
                    zipfile.ZipFile(next(Path(direct).glob("*.whl"))) as original:
                resource = "shared/_resources/host.json"
                if original.read(resource) != rebuilt.read(resource):
                    raise ValueError("sdist rebuild differs from direct wheel")
        sys.path.insert(0, str(ROOT))
        from tools.build_release import build_release
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        build_release(ROOT, staging / f"Tiffany-server-{version}.zip")
        artifacts = sorted(p for p in staging.iterdir() if p.is_file())
        for path in artifacts:
            validate(path)
        inventory = []
        expected = {p.name for p in artifacts}
        for previous in output.iterdir():
            if (previous.suffix in (".whl", ".zip") or previous.name.endswith(".tar.gz")) and previous.name not in expected:
                raise ValueError("output contains another release; select an empty --output directory")
        for path in artifacts:
            shutil.copyfile(path, output / path.name)
            inventory.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
        shutil.copyfile(ROOT / "tools/installed_acceptance.py", output / "installed_acceptance.py")
        inventory.append(f"{hashlib.sha256((output / 'installed_acceptance.py').read_bytes()).hexdigest()}  installed_acceptance.py\n")
        (output / "SHA256SUMS").write_text("".join(inventory), encoding="ascii")
    print(f"Validated release artifacts and SHA256SUMS: {output}")


if __name__ == "__main__":
    main()
