from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from settings import load_config
from shared.installation import HostInstallation, application_command
from .console import configure_utf8_output, utf8_environment
from .credentials import CredentialStore
from .environment import prepare_environment
from .locking import InstanceLock
from .logging import setup_logging
from .paths import PROJECT_ROOT, RuntimePaths
from .supervisor import Supervisor

logger = logging.getLogger(__name__)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Tiffany server launcher")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "init", "configure", "login", "check-config", "plugins"))
    parser.add_argument("plugin_args", nargs="*", help="plugins list|enable ID|disable ID|sync|check|repair")
    parser.add_argument("--home", help="runtime directory (source: project/data; installed: ./data)")
    parser.add_argument("--version", action="store_true", help="show Tiffany version without preparing an instance")
    parser.add_argument("--import-config", help="import an old TOML file; configure only")
    args = parser.parse_args(argv)
    if args.version:
        return args
    if args.import_config and args.command != "configure":
        parser.error("--import-config requires configure")
    if args.command == "plugins":
        values = args.plugin_args
        if (not values or values[0] not in ("list", "enable", "disable", "sync", "check", "repair")
                or len(values) != (2 if values[0] in ("enable", "disable") else 1)):
            parser.error("use plugins list|enable ID|disable ID|sync|check|repair")
    elif args.plugin_args:
        parser.error("unexpected arguments")
    return args


def main(argv=None) -> int:
    configure_utf8_output()
    args = arguments(argv)
    if sys.version_info < (3, 11):
        print("Tiffany requires Python 3.11+", file=sys.stderr)
        return 2
    try:
        if args.version:
            print(f"Tiffany {HostInstallation.detect().version}")
            return 0
        paths = RuntimePaths.resolve(args.home)
        if args.import_config:
            args.import_config = str(Path(args.import_config).expanduser().resolve())
        paths.initialize()
        with InstanceLock(paths.run / "launcher.lock"):
            if args.command == "plugins":
                from .plugin_commands import run_plugin_command
                return run_plugin_command(paths, args.plugin_args[0],
                                          args.plugin_args[1] if len(args.plugin_args) > 1 else None)
            if not paths.config.exists() and (args.command == "check-config" or
                    args.command in ("run", "init") and not sys.stdin.isatty()):
                entry = r".\start.bat" if sys.platform == "win32" else "bash start.sh"
                raise ValueError(f"配置不存在：{paths.config}。请执行 tiffany init --home \"{paths.home}\"；"
                                 f"源码入口也可使用 {entry} configure（使用相同 --home）。")
            print(f"Tiffany home: {paths.home}")
            setup_logging(paths.logs / "launcher.log", secrets=CredentialStore(paths).secrets())
            needs_config = args.command in ("configure", "login") or not paths.config.exists()
            if needs_config:
                with InstanceLock(paths.run / "application.lock"):
                    python = sys.executable if args.import_config else prepare_environment(paths)
                command = (application_command(python, "deployment.configure", "--home", str(paths.home))
                           if not args.import_config else
                           [str(python), "-B", "-X", "utf8", "-m", "deployment.configure", "--home", str(paths.home)])
                if args.command == "login":
                    command.append("--login")
                if args.import_config:
                    command.extend(["--import-config", args.import_config])
                code = subprocess.call(command, cwd=paths.home if not args.import_config else PROJECT_ROOT, env=utf8_environment())
                if code or args.command in ("configure", "login"):
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
            from shared.plugins import discover_plugins
            from .plugin_environment import sync_plugins
            with InstanceLock(paths.run / "application.lock"):
                plan = discover_plugins(paths)
                python = prepare_environment(paths)
                sync_plugins(paths, python, plan)
            if args.command == "init":
                print(f"实例已就绪：{paths.home}。运行 tiffany run --home \"{paths.home}\" 启动。")
                return 0
            environment = dict(utf8_environment(), TIFFANY_HOME=str(paths.home),
                               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
            return Supervisor(config.restart).run(
                application_command(python, "deployment.app", "--home", str(paths.home)),
                cwd=paths.home, env=environment,
            )
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError) as error:
        # Parsing errors contain locations and schema names, never credential values.
        print(f"启动失败：{error}", file=sys.stderr)
        return 2
