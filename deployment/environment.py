from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import sysconfig
import venv

from .paths import PROJECT_ROOT, RuntimePaths
from .package_process import run_package_command

logger = logging.getLogger(__name__)
IMPORT_CHECK = "import websockets, aiohttp, prometheus_client, qrcode; from qqbot_agent_sdk.onboard import start_onboard"


def environment_location(paths: RuntimePaths, lock: Path | None = None):
    lock = lock or PROJECT_ROOT / "requirements-server.lock"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    version = f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    platform = sysconfig.get_platform()
    environment = paths.envs / f"{version}-{platform}-{digest[:16]}"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    identity = {"python": version, "platform": platform, "lock_sha256": digest}
    return environment, python, identity


def prepare_environment(paths: RuntimePaths, lock: Path | None = None) -> Path:
    if sys.version_info < (3, 11):
        raise ValueError("Tiffany requires Python 3.11+")
    lock = lock or PROJECT_ROOT / "requirements-server.lock"
    environment, python, identity = environment_location(paths, lock)
    marker = environment / ".tiffany-ready.json"
    try:
        reusable = json.loads(marker.read_text()) == identity
    except (FileNotFoundError, ValueError):
        reusable = False
    if reusable and python.exists():
        result = subprocess.run([str(python), "-B", "-c", IMPORT_CHECK], capture_output=True)
        if result.returncode == 0:
            logger.info("复用 Python 环境：%s", environment.name)
            return python
        raise ValueError("已准备的 Python 环境损坏，请移走该环境目录后重试。")

    logger.info("准备 Python 环境：%s", environment.name)
    marker.unlink(missing_ok=True)
    try:
        venv.EnvBuilder(with_pip=False).create(environment)
        run_package_command([str(python), "-B", "-m", "ensurepip", "--upgrade"],
                            paths.logs / "environment-install.log")
        run_package_command([
            str(python), "-B", "-m", "pip", "install", "--require-hashes",
            "--only-binary=:all:", "--disable-pip-version-check", "--no-input",
            "--cache-dir", str(paths.cache / "pip"), "-r", str(lock),
        ], paths.logs / "environment-install.log")
        subprocess.run([str(python), "-B", "-c", IMPORT_CHECK], check=True)
        marker.write_text(json.dumps(identity) + "\n", encoding="utf-8")
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        raise ValueError("环境准备失败，未标记可用；修复网络/Python venv 后重新启动。") from error
    return python
