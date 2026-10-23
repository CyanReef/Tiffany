"""Artifact-only acceptance runner. Copy this script beside a wheel; no checkout needed."""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import venv
import zipfile


def executable(environment, name="python"):
    return environment / (f"Scripts/{name}.exe" if os.name == "nt" else f"bin/{name}")


def run(command, *, cwd=None, expected=0, timeout=180):
    result = subprocess.run([str(x) for x in command], cwd=cwd, input="", capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != expected:
        raise AssertionError(f"{command}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def wheel_write(target, entries, info):
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for name, data in sorted(entries.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + checksum, len(data)))
    writer.writerow((info + "/RECORD", "", ""))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr(info + "/RECORD", rows.getvalue().encode())


BACKEND = '''
from pathlib import Path
import base64, csv, hashlib, io, tomllib, zipfile
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    root = Path(__file__).parent.resolve()
    project = tomllib.loads((root/'pyproject.toml').read_text())['project']
    name, version = project['name'].replace('-','_'), project['version']
    info = f'{name}-{version}.dist-info'
    files = {name+'.pth': ('import sys; sys.path.append('+ascii(str(root))+')\\n').encode('ascii'),
      info+'/METADATA': ('Metadata-Version: 2.1\\nName: '+project['name']+'\\nVersion: '+version+'\\n'+''.join('Requires-Dist: '+r+'\\n' for r in project.get('dependencies',[]))).encode(),
      info+'/WHEEL': b'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n',
      info+'/entry_points.txt': b'[tiffany.plugins]\\nprobe = probe_plugin:install\\n'}
    rows = io.StringIO(newline=''); writer=csv.writer(rows,lineterminator='\\n')
    wheel=f'{name}-{version}-py3-none-any.whl'
    with zipfile.ZipFile(Path(wheel_directory)/wheel,'w') as z:
        for p,data in files.items():
            z.writestr(p,data); writer.writerow((p,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),len(data)))
        writer.writerow((info+'/RECORD','','')); z.writestr(info+'/RECORD',rows.getvalue())
    return wheel
build_wheel = build_editable
'''

PLUGIN = '''
import asyncio, signal
from fixture_dependency import reply
def install(scope, config):
    @scope.hook(on='message')
    async def probe(ctx):
        if ctx.raw.get('message') == '/probe':
            await ctx.reply(reply())
        elif ctx.raw.get('message') == '/stop':
            await ctx.reply('stopping')
            asyncio.get_running_loop().call_later(.2, signal.raise_signal, signal.SIGINT)
'''


def create_plugin(home, wheelhouse):
    root = home / "plugins/probe"
    (root / "probe_plugin").mkdir(parents=True)
    (root / "probe_plugin/__init__.py").write_text(PLUGIN, encoding="utf-8")
    (root / "backend.py").write_text(BACKEND, encoding="utf-8")
    (root / "pyproject.toml").write_text('''[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]
[project]
name = "tiffany-acceptance-probe"
version = "0.1.0"
dependencies = ["fixture-dependency==0.1.0"]
[project.entry-points."tiffany.plugins"]
probe = "probe_plugin:install"
[tool.tiffany.plugin]
api = 1
''', encoding="utf-8")
    info = "fixture_dependency-0.1.0.dist-info"
    wheel_write(wheelhouse / "fixture_dependency-0.1.0-py3-none-any.whl", {
        "fixture_dependency/__init__.py": b"def reply(): return 'dependency:ok'\n",
        info + "/METADATA": b"Metadata-Version: 2.1\nName: fixture-dependency\nVersion: 0.1.0\n",
        info + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }, info)
    return root


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


async def exercise(command, home, port, monitor):
    from websockets.asyncio.client import connect
    import urllib.request
    output = home / "logs/acceptance-process.log"
    with output.open("wb") as stream:
        child = subprocess.Popen([str(command), "run", "--home", str(home)], stdout=stream, stderr=stream)
        try:
            connection = None
            for _ in range(600):
                if child.poll() is not None:
                    raise AssertionError(output.read_text(encoding="utf-8", errors="replace"))
                try:
                    connection = await connect(f"ws://127.0.0.1:{port}", open_timeout=.2)
                    break
                except (OSError, TimeoutError):
                    await asyncio.sleep(.05)
            assert connection is not None, "application never listened"
            async with connection:
                for text, expected in (("ping", "pong"), ("/probe", "dependency:ok")):
                    await connection.send(json.dumps({"post_type": "message", "message_type": "group", "group_id": 7,
                                                       "user_id": 1, "self_id": 99, "message": text}))
                    result = json.loads(await asyncio.wait_for(connection.recv(), 8))
                    assert result["params"]["message"] == expected, result
                    await connection.send(json.dumps({"echo": result["echo"], "status": "ok", "retcode": 0,
                                                       "data": {"message_id": 1}}))
                for route in ("livez", "readyz", "metrics"):
                    def check():
                        with urllib.request.urlopen(f"http://127.0.0.1:{monitor}/{route}", timeout=5) as response:
                            assert response.status == 200
                    await asyncio.to_thread(check)
                await connection.send(json.dumps({"post_type": "message", "message_type": "group", "group_id": 7,
                                                   "user_id": 1, "self_id": 99, "message": "/stop"}))
                result = json.loads(await asyncio.wait_for(connection.recv(), 8))
                assert result["params"]["message"] == "stopping"
                await connection.send(json.dumps({"echo": result["echo"], "status": "ok", "retcode": 0,
                                                   "data": {"message_id": 2}}))
            assert await asyncio.to_thread(child.wait, 30) == 0, output.read_text(encoding="utf-8", errors="replace")
        finally:
            if child.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
                else:
                    child.terminate()
                child.wait(timeout=15)


def probe(home, wheelhouse, *, identity_only=False):
    from unittest.mock import patch
    from deployment.configure import wizard, commit_config
    from deployment.environment import environment_location
    from deployment.launcher import main
    from shared.installation import HostInstallation
    from shared.paths import RuntimePaths
    paths = RuntimePaths.resolve(home)
    host = HostInstallation.detect()
    assert host.mode == "wheel"
    assert paths.home == home
    command = executable(Path(sys.prefix), "tiffany")
    if identity_only:
        assert main(["init", "--home", str(home)]) == 0
        print("ENVIRONMENT=" + str(environment_location(paths)[0]))
        return
    assert RuntimePaths.resolve().home == (Path.cwd() / "data").resolve()
    for argument in ("--help", "--version"):
        run([command, argument, "--home", home])
        assert not home.exists()
    for argument in ("run", "init", "check-config"):
        run([command, argument, "--home", home], expected=2)
    assert not list(paths.envs.iterdir())
    paths.initialize()
    # Cancelling onboarding may leave prepared dependencies, but no credentials.
    with patch("sys.stdin.isatty", return_value=True), patch("deployment.launcher.subprocess.call", return_value=0):
        assert main(["init", "--home", str(home)]) == 0
    assert not paths.config.exists() and not paths.credentials.exists()
    plugin = create_plugin(home, wheelhouse)
    port, monitor = free_port(), free_port()

    def configure(command, **kwargs):
        assert "-I" in command and "deployment.configure" in command
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["Tiffany", "1", "127.0.0.1", str(port)]), \
                patch("getpass.getpass", return_value=""):
            wizard(paths)
        return 0

    with patch("sys.stdin.isatty", return_value=True), patch("deployment.launcher.subprocess.call", side_effect=configure):
        assert main(["init", "--home", str(home)]) == 0
    # Explicit configuration, equivalent to a user enabling monitoring after onboarding.
    from dataclasses import asdict
    from settings import load_config
    data = asdict(load_config(paths.config))
    data["monitoring"].update(enabled=True, host="127.0.0.1", port=monitor)
    commit_config(paths, data)
    protected = {paths.config: paths.config.read_bytes(), paths.credentials: paths.credentials.read_bytes(),
                 paths.storage / "business.bin": b"business-state"}
    (paths.storage / "business.bin").write_bytes(b"business-state")
    run([command, "init", "--home", home])
    assert all(p.read_bytes() == content for p, content in protected.items())
    run([command, "check-config", "--home", home])
    run([command, "plugins", "check", "--home", home])
    environment, python, _ = environment_location(paths)
    assert python.is_file()
    asyncio.run(exercise(command, home, port, monitor))
    for action in ("disable", "enable"):
        run([command, "plugins", action, "probe", "--home", home])
        run([command, "init", "--home", home])
    declaration = plugin / "pyproject.toml"
    original = declaration.read_bytes()
    declaration.write_bytes(original.replace(b"fixture-dependency==0.1.0", b"fixture-dependency==9.0.0"))
    run([command, "plugins", "sync", "--home", home], expected=2)
    declaration.write_bytes(original)
    marker = environment / ".tiffany-plugins.json"
    marker.write_text("{broken", encoding="utf-8")
    run([command, "run", "--home", home], expected=2)
    run([command, "plugins", "sync", "--home", home])
    cached = host.wheel(paths.cache)
    cached.write_bytes(b"broken cache")
    assert zipfile.is_zipfile(host.wheel(paths.cache))
    # Source and runtime corruption are distinct from a recoverable wheel cache.
    from shared.installation import owned_distribution
    manifest = host.manifest
    candidate = next(p for p in manifest["files"] if p.endswith(".py") and p != "shared/installation.py")
    source = Path(owned_distribution().locate_file(candidate))
    before = source.read_bytes()
    try:
        source.write_bytes(before + b"\n# corruption\n")
        try:
            host.wheel(paths.cache)
        except ValueError:
            pass
        else:
            raise AssertionError("corrupted installed host was accepted")
    finally:
        source.write_bytes(before)
    run([command, "plugins", "repair", "--home", home])
    assert all(p.read_bytes() == content for p, content in protected.items())
    run([command, "init", "--home", home])
    # An unprepared second instance must fail cleanly when installation material is absent.
    missing = RuntimePaths.resolve(home.parent / "missing-dependencies")
    missing.initialize()
    missing.config.write_bytes(paths.config.read_bytes())
    empty = home.parent / "empty-wheelhouse"
    empty.mkdir()
    with patch.dict(os.environ, {"PIP_FIND_LINKS": empty.as_uri()}):
        run([command, "init", "--home", missing.home], expected=2)
    assert not list(missing.envs.glob("*/.tiffany-ready.json"))
    print("ENVIRONMENT=" + str(environment))
    print("PASS: init, real launcher ping/plugin/health/stop, offline reuse, conflicts, corruption and repair")


def variant(wheel, directory, *, bump=False):
    with zipfile.ZipFile(wheel) as archive:
        entries = {p: archive.read(p) for p in archive.namelist() if not p.endswith("/RECORD")}
    manifest_path = "shared/_resources/host.json"
    manifest = json.loads(entries[manifest_path])
    old = manifest["version"]
    version = old + ".post1" if bump else old
    original_info = f"tiffany_hook_runtime-{old}.dist-info"
    info = f"tiffany_hook_runtime-{version}.dist-info"
    entries = {p.replace(original_info + "/", info + "/"): data for p, data in entries.items()}
    entries[info + "/METADATA"] = entries[info + "/METADATA"].replace(
        f"Version: {old}".encode(), f"Version: {version}".encode(), 1)
    entries["shared/installation.py"] += b"\n# artifact acceptance variant\n"
    manifest["version"] = version
    manifest["files"]["shared/installation.py"] = hashlib.sha256(entries["shared/installation.py"]).hexdigest()
    entries[manifest_path] = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    directory.mkdir()
    target = directory / f"tiffany_hook_runtime-{version}-py3-none-any.whl"
    wheel_write(target, entries, info)
    return target


def main():
    for stream in (sys.stdout, sys.stderr):
        if reconfigure := getattr(stream, "reconfigure", None):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--identity-only", action="store_true")
    parser.add_argument("--uv", action="store_true", help="also validate isolated uv tool installation")
    args = parser.parse_args()
    if args.probe:
        probe(args.probe.resolve(), args.wheelhouse.resolve(), identity_only=args.identity_only)
        return
    assert args.wheel and args.wheel.is_file()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="Tiffany installed 中文 ") as temporary:
        root = Path(temporary).resolve()
        wheelhouse = root / "wheelhouse"
        shutil.copytree(args.wheelhouse.resolve(), wheelhouse)
        script = root / "acceptance.py"
        shutil.copyfile(__file__, script)
        os.environ.pop("PYTHONPATH", None)
        os.environ.pop("PYTHONHOME", None)
        os.environ.pop("TIFFANY_HOME", None)
        os.environ.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=wheelhouse.as_uri(), PIP_DISABLE_PIP_VERSION_CHECK="1",
                          PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        environment = root / "tool"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = executable(environment)
        run([python, "-I", "-m", "pip", "install", wheel], cwd=root)
        home = root / "instance 中文/data"
        home.parent.mkdir()
        command = [python, "-I", "-B", "-X", "utf8", script, "--probe", home, "--wheelhouse", wheelhouse]
        print(run(command, cwd=root), flush=True)
        configs = {p: p.read_bytes() for p in (home / "config/Tiffany.toml", home / "secrets/credentials.json", home / "storage/business.bin")}
        def identity():
            output = run([*command, "--identity-only"], cwd=root)
            return next(line[12:] for line in output.splitlines() if line.startswith("ENVIRONMENT="))
        old = identity()
        for bump in (True, False):
            changed = variant(wheel, root / ("new-version" if bump else "same-version"), bump=bump)
            run([python, "-I", "-m", "pip", "install", "--no-deps", "--force-reinstall", changed], cwd=root)
            assert identity() != old
            run([python, "-I", "-m", "pip", "install", "--no-deps", "--force-reinstall", wheel], cwd=root)
            assert identity() == old
            assert all(p.read_bytes() == content for p, content in configs.items())
        print("PASS: upgrade/rollback and same-version different-content environment selection", flush=True)
        if args.uv:
            uv = shutil.which("uv")
            assert uv, "uv is required for --uv acceptance"
            os.environ.update(UV_TOOL_DIR=str(root / "uv-tools"), UV_TOOL_BIN_DIR=str(root / "uv-bin"),
                              UV_CACHE_DIR=str(root / "uv-cache"), UV_PYTHON_DOWNLOADS="never")
            run([uv, "tool", "install", "--python", sys.executable, "--no-index", "--find-links", wheelhouse, wheel], cwd=root)
            tool = root / "uv-bin" / ("tiffany.exe" if os.name == "nt" else "tiffany")
            run([tool, "--version"], cwd=root)
            run([tool, "init", "--home", home], cwd=root)
            run([tool, "plugins", "check", "--home", home], cwd=root)
            print("PASS: isolated uv installation reuses the instance without changing data", flush=True)


if __name__ == "__main__":
    main()
