"""Incremental plugin synchronization in the server's managed environment."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import tempfile
import tomllib

from shared.plugins import PluginError, PluginPlan, canonical_name, discover_plugins
from .environment import environment_location, prepare_environment
from .package_process import run_package_command
from .paths import PROJECT_ROOT

logger = logging.getLogger(__name__)
MARKER = ".tiffany-plugins.json"


def read_state(environment: Path) -> dict | None:
    try:
        value = json.loads((environment / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        return {"status": "dirty", "managed": []}
    if (not isinstance(value, dict) or value.get("status") not in ("ready", "dirty")
            or not isinstance(value.get("identity"), dict)
            or not isinstance(value.get("managed"), list)
            or any(not isinstance(name, str) for name in value["managed"])):
        return {"status": "dirty", "managed": []}
    return value


def write_state(environment: Path, state: dict) -> None:
    with tempfile.NamedTemporaryFile("w", dir=environment, prefix=".plugins-", delete=False, encoding="utf-8") as stream:
        staged = Path(stream.name)
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(staged, environment / MARKER)
    finally:
        staged.unlink(missing_ok=True)


def environment_identity(python: Path, plan: PluginPlan, root=PROJECT_ROOT) -> dict:
    return {**plan.identity(root), "python": str(python.resolve()),
            "implementation": sys.implementation.name, "version": list(sys.version_info[:3]),
            "platform": sysconfig.get_platform()}


def base_constraints(root: Path) -> str:
    """Keep PEP 508 markers, but do not propagate hash mode into editable installs."""
    lines = []
    for line in (root / "requirements-server.lock").read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", line):
            lines.append(line.rstrip().removesuffix("\\").rstrip())
    host = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lines.append(f"{host['name']}=={host['version']}")
    return "\n".join(lines) + "\n"


def verify_prepared(paths, plan: PluginPlan, *, environment: Path | None = None) -> None:
    """Offline startup check. Direct main.py never installs dependencies."""
    environment = Path(sys.prefix) if environment is None else environment
    state = read_state(environment)
    if not plan.enabled and state is None:
        return
    if (state is None or state.get("status") != "ready"
            or any(state.get("identity", {}).get(key) != value for key, value in plan.identity().items())):
        raise PluginError("environment", "readiness", "use the normal launcher or plugins sync/repair")
    current_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    identity = environment_identity(current_python, plan)
    if state["identity"] != identity:
        raise PluginError("environment", "readiness", "interpreter changed; run plugins sync")


def sync_plugins(paths, python: Path, plan: PluginPlan, *, force=False, root=PROJECT_ROOT) -> None:
    """Caller must hold launcher and application locks for all mutations."""
    environment = python.parent.parent
    state = read_state(environment)
    identity = environment_identity(python, plan, root)
    if not plan.enabled and state is None:
        return
    if not force and state and state.get("status") == "ready" and state.get("identity") == identity:
        logger.info("复用插件环境：%d 个启用插件", len(plan.enabled))
        return
    if state and state.get("status") != "ready" and not force:
        raise PluginError("environment", "readiness", "previous synchronization failed; run plugins sync or plugins repair")
    log = paths.logs / "plugins-install.log"
    logger.info("准备插件依赖：%s；诊断：%s", ", ".join(s.id for s in plan.enabled) or "none", log)
    with tempfile.TemporaryDirectory(prefix="plugin-sync-", dir=paths.cache) as temporary:
        temporary = Path(temporary)
        constraints = temporary / "constraints.txt"
        constraint_text = base_constraints(root)
        reserved = {canonical_name(line.split("==", 1)[0]) for line in constraint_text.splitlines()}
        for spec in plan.enabled:
            if spec.name in reserved:
                raise PluginError(spec.id, "resolution", "plugin distribution name collides with a host dependency")
        constraints.write_text(constraint_text, encoding="utf-8")
        desired = temporary / "plan.json"
        desired.write_text(json.dumps({"host": str(root), "plugins": [s.record() for s in plan.enabled]}), encoding="utf-8")
        command = [str(python), "-B", "-m", "pip", "--disable-pip-version-check", "--no-input"]
        install = ["install", "--cache-dir", str(paths.cache / "pip"), "-c", str(constraints), "-e", str(root)]
        for spec in plan.enabled:
            install.extend(["-e", str(spec.root)])
        try:
            # Resolve all selected projects together before changing installed packages.
            run_package_command([*command, *install, "--dry-run", "--report", str(temporary / "resolution.json")], log)
        except (ValueError, OSError) as error:
            raise PluginError(",".join(s.id for s in plan.enabled) or "environment", "resolution", str(error)) from error
        old_managed = state.get("managed", []) if state else []
        target = {s.name for s in plan.enabled}
        # Keep prior managed names in a dirty record so a retry can finish removals.
        pending = sorted(set(old_managed) | target)
        write_state(environment, {"status": "dirty", "identity": identity, "managed": pending})
        try:
            removed = sorted(set(old_managed) - target)
            if removed:
                run_package_command([*command, "uninstall", "-y", *removed], log)
            run_package_command([*command, *install], log)
            run_package_command([*command, "check"], log)
            inventory = run_package_command(
                [str(python), "-B", "-m", "deployment.plugin_worker", "inspect", str(desired)], log, capture=True)
            installed = json.loads(inventory)
            # An editor may have changed declarations while pip was working.
            if root == PROJECT_ROOT and environment_identity(python, discover_plugins(paths)) != identity:
                raise ValueError("plugin declarations changed during synchronization; retry plugins sync")
            write_state(environment, {"status": "ready", "identity": identity,
                                      "managed": sorted(target), "installed": installed})
        except (ValueError, OSError) as error:
            raise PluginError(",".join(s.id for s in plan.enabled) or "environment", "installation", str(error)) from error


def repair_environment(paths) -> Path:
    """Recreate only the resolved managed environment, with both instance locks held."""
    environment, _, _ = environment_location(paths)
    target, parent = environment.resolve(), paths.envs.resolve()
    if environment.is_symlink() or target.parent != parent or target == Path(sys.prefix).resolve():
        raise PluginError("environment", "repair", "refusing to remove an unsafe or currently executing environment")
    if target.exists():
        shutil.rmtree(target)
    return prepare_environment(paths)
