from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import sysconfig
import venv

from shared.installation import HostInstallation, application_command
from .paths import PROJECT_ROOT, RuntimePaths
from .package_process import run_package_command

logger = logging.getLogger(__name__)
IMPORT_CHECK = "import websockets, aiohttp, prometheus_client, qrcode; from qqbot_agent_sdk.onboard import start_onboard"


def environment_location(paths: RuntimePaths, lock: Path | None = None):
    host = HostInstallation.detect()
    digest = hashlib.sha256(lock.read_bytes() if lock else host.lock_bytes).hexdigest()
    version = f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    platform = sysconfig.get_platform()
    host_digest = hashlib.sha256(json.dumps(host.identity(), sort_keys=True).encode()).hexdigest()
    environment = paths.envs / f"{version}-{platform}-{digest[:16]}-{host_digest[:16]}"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    identity = {"schema": 2, "python": version, "platform": platform, "lock_sha256": digest,
                "host": host.identity()}
    return environment, python, identity


def prepare_environment(paths: RuntimePaths, lock: Path | None = None) -> Path:
    if sys.version_info < (3, 11):
        raise ValueError("Tiffany requires Python 3.11+")
    host = HostInstallation.detect()
    environment, python, identity = environment_location(paths, lock)
    marker = environment / ".tiffany-ready.json"
    try:
        reusable = json.loads(marker.read_text()) == identity
    except (FileNotFoundError, ValueError):
        reusable = False
    check = (IMPORT_CHECK + "; import json; from deployment.plugin_worker import inspect_installation; "
             + "inspect_installation(json.loads(" + repr(json.dumps({"host": host.identity(), "plugins": []})) + "))")
    if reusable and python.exists():
        result = subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-c", check], capture_output=True)
        if result.returncode == 0:
            logger.info("复用 Python 环境：%s", environment.name)
            return python
        raise ValueError(f"已准备的 Python 环境损坏，请执行 tiffany plugins repair --home \"{paths.home}\"。")

    logger.info("准备 Python 环境：%s", environment.name)
    marker.unlink(missing_ok=True)
    try:
        venv.EnvBuilder(with_pip=False).create(environment)
        run_package_command(application_command(python, "ensurepip", "--upgrade"),
                            paths.logs / "environment-install.log")
        lock_path = lock or paths.cache / "locks" / (identity["lock_sha256"] + ".txt")
        if lock is None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_bytes(host.lock_bytes)
        run_package_command([
            *application_command(python, "pip"), "install", "--require-hashes",
            "--only-binary=:all:", "--disable-pip-version-check", "--no-input",
            "--cache-dir", str(paths.cache / "pip"), "-r", str(lock_path),
        ], paths.logs / "environment-install.log")
        run_package_command([*application_command(python, "pip"), "install", "--no-deps",
                             "--disable-pip-version-check", "--no-input",
                             "--cache-dir", str(paths.cache / "pip"), *host.install_arguments(paths.cache)],
                            paths.logs / "environment-install.log")
        run_package_command(application_command(python, "pip", "check"), paths.logs / "environment-install.log")
        subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-c", check], check=True)
        host.verify()
        from .plugin_environment import write_state
        # Use the same atomic replacement primitive, with an independent marker.
        write_state(environment, identity, marker_name=marker.name)
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        raise ValueError("环境准备失败，未标记可用；修复网络/Python venv 后重新启动。") from error
    return python
