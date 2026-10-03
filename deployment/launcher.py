from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

from settings import load_config
from .credentials import CredentialStore
from .environment import prepare_environment
from .locking import InstanceLock
from .logging import setup_logging
from .paths import PROJECT_ROOT, RuntimePaths
from .supervisor import Supervisor

logger = logging.getLogger(__name__)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Tiffany server launcher")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "configure", "login", "check-config"))
    parser.add_argument("--home", help="runtime directory (default: project/data)")
    parser.add_argument("--import-config", help="import an old TOML file; configure only")
    args = parser.parse_args(argv)
    if args.import_config and args.command != "configure":
        parser.error("--import-config requires configure")
    return args


def main(argv=None) -> int:
    args = arguments(argv)
    if sys.version_info < (3, 11):
        print("Tiffany requires Python 3.11+", file=sys.stderr)
        return 2
    paths = RuntimePaths.resolve(args.home)
    try:
        paths.initialize()
        with InstanceLock(paths.run / "launcher.lock"):
            if args.command == "run" and not paths.config.exists() and not sys.stdin.isatty():
                entry = r".\start.bat" if sys.platform == "win32" else "bash start.sh"
                raise ValueError(f"配置不存在。请先在交互终端执行 {entry} configure（使用相同 --home）。")
            setup_logging(paths.logs / "launcher.log", secrets=CredentialStore(paths).secrets())
            needs_config = args.command in ("configure", "login") or not paths.config.exists()
            if needs_config:
                python = sys.executable if args.import_config else prepare_environment(paths)
                command = [str(python), "-B", "-m", "deployment.configure", "--home", str(paths.home)]
                if args.command == "login":
                    command.append("--login")
                if args.import_config:
                    command.extend(["--import-config", args.import_config])
                code = subprocess.call(command, cwd=PROJECT_ROOT)
                if code or args.command != "run":
                    return code
                if not paths.config.exists():
                    return 0  # First-run wizard cancelled.
            config = load_config(paths.config)
            store = CredentialStore(paths)
            secrets = (*store.secrets(), *config.validate_credentials(store.resolve))
            setup_logging(paths.logs / "launcher.log", level=config.logging.level,
                          max_bytes=config.logging.max_bytes, backups=config.logging.backups, secrets=secrets)
            if args.command == "check-config":
                logger.info("配置及凭据完整，未连接平台：%s", paths.config)
                return 0
            python = prepare_environment(paths)
            environment = dict(os.environ, TIFFANY_HOME=str(paths.home), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
            return Supervisor(config.restart).run(
                [str(python), "-B", str(PROJECT_ROOT / "main.py"), "--home", str(paths.home)],
                cwd=PROJECT_ROOT, env=environment,
            )
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError) as error:
        # Parsing errors contain locations and schema names, never credential values.
        print(f"启动失败：{error}", file=sys.stderr)
        return 2
