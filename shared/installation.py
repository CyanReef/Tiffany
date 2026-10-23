"""Read-only host identity and local wheel materialization; standard library only."""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
import hashlib
import importlib.metadata as metadata
from importlib.resources import files
import io
import json
import os
from pathlib import Path, PurePosixPath
import sysconfig
import tempfile
import tomllib
import zipfile
import zlib

NAME = "tiffany-hook-runtime"
RESOURCE = "shared/_resources/"
MANIFEST = RESOURCE + "host.json"
LOCK = RESOURCE + "requirements-server.lock"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def production_sources(root: Path) -> dict[str, Path]:
    """The allowlist shared by source identity and wheel construction."""
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    options = config.get("tool", {}).get("setuptools", {})
    result = {}
    for package in options.get("packages", []):
        folder = root / package.replace(".", "/")
        for source in folder.glob("*.py"):
            result[source.relative_to(root).as_posix()] = source
    for module in options.get("py-modules", []):
        source = root / (module.replace(".", "/") + ".py")
        result[source.relative_to(root).as_posix()] = source
    # Small custom-backend projects are useful for host compatibility tests.
    if not options:
        result.update({p.relative_to(root).as_posix(): p for p in root.glob("*/*.py")})
    result[LOCK] = root / "requirements-server.lock"
    if (root / "LICENSE").is_file():
        result[RESOURCE + "LICENSE"] = root / "LICENSE"
    for path in result.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid host resource: {path}")
    return result


def source_manifest(root: Path) -> dict:
    project_bytes = (root / "pyproject.toml").read_bytes()
    project = tomllib.loads(project_bytes.decode("utf-8"))["project"]
    if project["name"] != NAME:
        raise ValueError("source is not a Tiffany project")
    return {"schema": 1, "name": NAME, "version": project["version"],
            "project_sha256": digest(project_bytes),
            "files": {name: digest(path.read_bytes()) for name, path in sorted(production_sources(root).items())}}


def owned_distribution():
    """Ignore neighboring source egg-info and attest the interpreter's installation."""
    paths = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib")}
    for dist in metadata.distributions(path=list(paths)):
        if dist.metadata.get("Name", "").lower().replace("_", "-") == NAME:
            return dist
    raise ValueError("Tiffany installed distribution metadata is missing")


@dataclass(frozen=True)
class HostInstallation:
    mode: str
    root: Path
    manifest: dict
    lock_bytes: bytes

    @classmethod
    def detect(cls, root: Path | None = None):
        actual = Path(__file__).resolve().parent.parent
        selected = Path(root).resolve() if root is not None else actual
        if root is not None or not files("shared").joinpath("_resources", "host.json").is_file():
            # Both code location and the project's declared package layout matter.
            manifest = source_manifest(selected)
            if root is None and "shared/installation.py" not in manifest["files"]:
                raise ValueError("Tiffany source layout is incomplete")
            return cls("source", selected, manifest, (selected / "requirements-server.lock").read_bytes())
        manifest = json.loads(files("shared").joinpath("_resources", "host.json").read_bytes())
        if (not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("name") != NAME
                or not isinstance(manifest.get("version"), str) or not isinstance(manifest.get("files"), dict)
                or LOCK not in manifest["files"]):
            raise ValueError("invalid Tiffany host manifest")
        host = cls("wheel", actual, manifest,
                   files("shared").joinpath("_resources", "requirements-server.lock").read_bytes())
        host.verify()
        return host

    @property
    def name(self):
        return self.manifest["name"]

    @property
    def version(self):
        return self.manifest["version"]

    @property
    def content_id(self):
        return digest(canonical(self.manifest))

    def identity(self):
        return {"mode": self.mode, "name": self.name, "version": self.version,
                "content_id": self.content_id, "source": str(self.root) if self.mode == "source" else None}

    def verify(self):
        if self.mode == "source":
            if source_manifest(self.root) != self.manifest:
                raise ValueError("host source changed during environment preparation")
            return
        dist = owned_distribution()
        if dist.version != self.version or Path(dist.locate_file("shared/installation.py")).resolve() != Path(__file__).resolve():
            raise ValueError("host metadata does not match imported Tiffany code")
        for name, expected in self.manifest["files"].items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise ValueError("invalid path in host manifest")
            if digest((self.root / name).read_bytes()) != expected:
                raise ValueError(f"host file integrity check failed: {name}")

    def wheel(self, cache: Path) -> Path:
        """Create an installation seed without looking up another distribution."""
        if self.mode != "wheel":
            raise ValueError("only an installed host can be materialized as a wheel")
        self.verify()
        dist = owned_distribution()
        entries = {name: (self.root / name).read_bytes() for name in self.manifest["files"]}
        entries[MANIFEST] = files("shared").joinpath("_resources", "host.json").read_bytes()
        info = f"tiffany_hook_runtime-{self.version}.dist-info"
        excluded = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "RECORD.jws", "RECORD.p7s"}
        for item in dist.files or ():
            parts = PurePosixPath(str(item).replace("\\", "/")).parts
            if parts and parts[0] == info and len(parts) > 1 and ".." not in parts and parts[-1] not in excluded:
                entries["/".join(parts)] = Path(dist.locate_file(item)).read_bytes()
        for required in ("METADATA", "WHEEL", "entry_points.txt"):
            if f"{info}/{required}" not in entries:
                raise ValueError(f"host metadata missing {required}")
        wheel_metadata = entries[info + "/WHEEL"].decode("utf-8")
        if "Root-Is-Purelib: true" not in wheel_metadata or "Tag: py3-none-any" not in wheel_metadata:
            raise ValueError("host wheel cache supports Tiffany pure Python distributions only")
        record = io.StringIO(newline="")
        writer = csv.writer(record, lineterminator="\n")
        for name, data in sorted(entries.items()):
            checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            writer.writerow((name, "sha256=" + checksum, len(data)))
        writer.writerow((info + "/RECORD", "", ""))
        entries[info + "/RECORD"] = record.getvalue().encode("utf-8")
        directory = cache / "host" / self.content_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"tiffany_hook_runtime-{self.version}-py3-none-any.whl"

        def matches(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    return (sorted(archive.namelist()) == sorted(entries)
                            and all(archive.read(name) == data for name, data in entries.items()))
            except (OSError, ValueError, zipfile.BadZipFile, KeyError, EOFError, zlib.error):
                return False

        if matches(target):
            return target
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".whl", delete=False) as stream:
            staged = Path(stream.name)
        try:
            with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(entries.items()):
                    archive.writestr(name, data)
            if not matches(staged):
                raise ValueError("host wheel validation failed")
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        return target

    def install_arguments(self, cache: Path):
        return ["-e", str(self.root)] if self.mode == "source" else [str(self.wheel(cache))]


def application_command(python: Path, module: str, *arguments: str) -> list[str]:
    return [str(python), "-I", "-B", "-u", "-X", "utf8", "-m", module, *arguments]
