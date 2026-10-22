"""Plugin management commands, usable before platform onboarding."""
from __future__ import annotations

from dataclasses import replace
import subprocess

from shared.plugins import PluginError, discover_plugins
from .console import utf8_environment
from .environment import environment_location, prepare_environment
from .locking import InstanceLock
from .paths import PROJECT_ROOT
from .plugin_environment import environment_identity, read_state, repair_environment, sync_plugins
from .storage import save_documents


def run_plugin_command(paths, operation: str, plugin_id: str | None) -> int:
    plan = discover_plugins(paths, validate=operation not in ("list", "enable", "disable"))
    _, python, _ = environment_location(paths)
    if operation == "list":
        state = read_state(python.parent.parent)
        try:
            plan = discover_plugins(paths)
        except PluginError:
            state = None
        ready = bool(state and state.get("status") == "ready" and state.get("identity") == environment_identity(python, plan))
        print("ID\t状态\t环境\t源码")
        for spec in plan.specs:
            enabled = spec.id not in plan.settings.disabled
            status = "ready" if ready else "dirty" if state and state.get("status") == "dirty" else "pending"
            print(f"{spec.id}\t{'enabled' if enabled else 'disabled'}\t{status}\t{spec.root}")
        if not plan.specs:
            print("未发现插件。将项目放入 " + str(paths.plugins))
        return 0
    # Shared environments must never be modified while a directly launched application runs.
    with InstanceLock(paths.run / "application.lock"):
        if operation in ("enable", "disable"):
            if plugin_id not in {spec.id for spec in plan.specs}:
                raise PluginError(plugin_id or "unknown", "configuration", "plugin was not discovered")
            disabled = set(plan.settings.disabled)
            if operation == "enable":
                disabled.discard(plugin_id)
            else:
                disabled.add(plugin_id)
            settings = replace(plan.settings, disabled=tuple(sorted(disabled)))
            save_documents({paths.plugin_settings: settings.encode()}, paths.backups)
            print(f"{plugin_id}: {operation}；下次启动生效。")
            return 0
        if operation == "check":
            if not python.is_file():
                raise PluginError("environment", "readiness", "run plugins sync first")
            return subprocess.call([str(python), "-B", "-m", "deployment.plugin_worker", "check", str(paths.home)],
                                   cwd=PROJECT_ROOT, env=utf8_environment())
        print(f"正在准备共享环境，安装诊断保存在 {paths.logs}")
        python = repair_environment(paths) if operation == "repair" else prepare_environment(paths)
        sync_plugins(paths, python, plan, force=True)
        print("插件依赖环境已就绪。")
        return 0
