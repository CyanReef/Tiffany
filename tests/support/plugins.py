"""Small real Python projects for plugin and package-manager acceptance tests."""
from pathlib import Path
import json


def installed_wheel(name, destination):
    """Repack locally installed build tooling for offline acceptance, never download it."""
    import base64
    import hashlib
    import importlib.metadata as metadata
    import site
    import sys
    import zipfile
    distributions = metadata.distributions(path=[*sys.path, *site.getsitepackages([sys.base_prefix])])
    dist = next((d for d in distributions if d.metadata.get("Name", "").lower() == name.lower()), None)
    if dist is None:
        raise metadata.PackageNotFoundError(name)
    wheel_metadata = dist.read_text("WHEEL") or "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    tag = next(line[5:] for line in wheel_metadata.splitlines() if line.startswith("Tag: "))
    wheel = destination / f"{name.replace('-', '_')}-{dist.version}-{tag}.whl"
    records = []
    info = f"{name.replace('-', '_')}-{dist.version}.dist-info"
    metadata_text = dist.read_text("METADATA") or dist.read_text("PKG-INFO")
    files = {f"{info}/METADATA": metadata_text.encode(), f"{info}/WHEEL": wheel_metadata.encode()}
    if dist.read_text("entry_points.txt"):
        files[f"{info}/entry_points.txt"] = dist.read_text("entry_points.txt").encode()
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in dist.files:
            if ".." in path.parts or str(path).endswith((".pyc", "/RECORD")):
                continue
            if path.parts[0].endswith((".dist-info", ".egg-info")):
                continue
            source = Path(dist.locate_file(path))
            if not source.is_file():
                continue
            files[str(path).replace("\\", "/")] = source.read_bytes()
        for path, data in files.items():
            archive.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            records.append(f"{path},sha256={digest},{len(data)}")
        archive.writestr(f"{info}/RECORD", "\n".join(records) + f"\n{info}/RECORD,,\n")
    return wheel


def plugin_project(root: Path, plugin_id="sample", *, name=None, requires=(), dependencies=(), code=None, src=False):
    root.mkdir(parents=True, exist_ok=True)
    module = (name or f"plugin_{plugin_id}").replace("-", "_")
    package = root / ("src" if src else ".") / module
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(code or "def install(scope, config):\n    pass\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        f'[project]\nname = {json.dumps(name or module)}\nversion = "0.1.0"\n'
        f'dependencies = {json.dumps(list(dependencies))}\n'
        f'[project.entry-points."tiffany.plugins"]\n{plugin_id} = "{module}:install"\n'
        f'[tool.tiffany.plugin]\napi = 1\nrequires = {json.dumps(list(requires))}\n', encoding="utf-8")
    (root / "backend.py").write_text(BACKEND, encoding="utf-8")
    return root


# A dependency-free PEP 517/660 backend keeps the real-pip tests fully offline.
BACKEND = r'''
from pathlib import Path
import base64, hashlib, json, tomllib, zipfile

def _build(wheel_directory, editable):
    root = Path(__file__).parent.resolve()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name = project["name"].replace("-", "_")
    version = project["version"]
    directory = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {req}\n" for req in project.get("dependencies", []))
    entries = project.get("entry-points", {}).get("tiffany.plugins", {})
    files = {
        f"{directory}/METADATA": metadata,
        f"{directory}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    source = root / "src" if (root / "src").is_dir() else root
    if editable:
        # Python 3.11/3.12 decode .pth using the Windows locale, not UTF-8.
        files[name + ".pth"] = "import sys; sys.path.append(" + ascii(str(source)) + ")\n"
    elif (source / name).is_dir():
        for path in (source / name).rglob("*.py"):
            files[path.relative_to(source).as_posix()] = path.read_text()
    if entries:
        files[f"{directory}/entry_points.txt"] = "[tiffany.plugins]\n" + "".join(f"{k} = {v}\n" for k,v in entries.items())
    records = []
    wheel = f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / wheel, "w") as archive:
        for path, text in files.items():
            data = text.encode()
            archive.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            records.append(f"{path},sha256={digest},{len(data)}")
        archive.writestr(f"{directory}/RECORD", "\n".join(records) + f"\n{directory}/RECORD,,\n")
    return wheel

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, True)

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, False)
'''
