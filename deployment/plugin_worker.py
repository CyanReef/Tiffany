"""Managed-interpreter metadata validation and offline registration checks."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import sysconfig
import tomllib
from urllib.parse import urlsplit
from urllib.request import url2pathname

from shared.plugins import GROUP, PluginError, canonical_name, discover_plugins
from shared.installation import HostInstallation
from .console import configure_utf8_output


def inspect_installation(desired: dict) -> dict:
    # Source-tree egg-info can shadow installed dist-info on sys.path. Only
    # interpreter-owned package directories can attest an editable installation.
    package_paths = list({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    distributions = {canonical_name(dist.metadata["Name"]): dist
                     for dist in metadata.distributions(path=package_paths) if dist.metadata.get("Name")}
    expected = desired["host"]
    if isinstance(expected, str):
        expected = HostInstallation.detect(Path(expected)).identity()
    host = {"name": expected["name"], "version": expected["version"], "root": expected.get("source")}
    specs = [host, *desired["plugins"]]
    for spec in specs:
        try:
            dist = distributions[canonical_name(spec["name"])]
            if dist.version != spec["version"]:
                raise ValueError("installed version does not match the declaration")
            if spec["root"] is not None:
                direct = json.loads(dist.read_text("direct_url.json") or "{}")
                url = urlsplit(direct.get("url", ""))
                source = Path(url2pathname(url.path))
                if url.netloc:
                    source = Path("//" + url.netloc + str(source))
                if (url.scheme != "file" or source.resolve() != Path(spec["root"]).resolve()
                        or not direct.get("dir_info", {}).get("editable")):
                    raise ValueError("installed editable source does not match the declaration")
            if spec is host and HostInstallation.detect().identity() != expected:
                raise ValueError("installed host content does not match the launcher")
            if "entry" in spec:
                entries = [ep for ep in dist.entry_points if ep.group == GROUP]
                if len(entries) != 1 or entries[0].name != spec["id"] or entries[0].value != spec["entry"]:
                    raise ValueError("installed entry point does not match pyproject.toml")
        except (KeyError, metadata.PackageNotFoundError, ValueError, OSError) as error:
            raise PluginError(spec.get("id", "host"), "metadata", str(error)) from error
    return {name: dist.version for name, dist in distributions.items()}


async def check_registration(paths) -> None:
    from core import APP_PATHS, Bot
    from hooks import install_extensions, register_hooks
    from hooks.plugins import install_plugins
    from .plugin_environment import verify_prepared

    plan = discover_plugins(paths)
    verify_prepared(paths, plan)
    bot = Bot()
    bot.service(APP_PATHS, paths)
    if paths.config.exists():
        from settings import load_config
        config = load_config(paths.config)
        if config.extensions is None:
            register_hooks(bot)
        else:
            await install_extensions(bot, config.extensions)
    try:
        await install_plugins(bot, paths, plan)
        bot.services.validate()
    finally:
        # Registration validation deliberately does not enter setup/start.
        await bot.stop("abort")


def main(argv=None) -> int:
    configure_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "check"))
    parser.add_argument("value")
    args = parser.parse_args(argv)
    try:
        if args.operation == "inspect":
            print(json.dumps(inspect_installation(json.loads(Path(args.value).read_text(encoding="utf-8")))))
        else:
            from shared.paths import RuntimePaths
            asyncio.run(check_registration(RuntimePaths.resolve(args.value)))
            print("插件入口、配置和注册关系校验通过；未启动资源或连接平台。")
        return 0
    except (ValueError, ImportError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
