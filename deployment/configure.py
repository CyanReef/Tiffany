from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import getpass
import json
from pathlib import Path
import sys
import tomllib

from settings import load_config, parse_config
from .credentials import CredentialStore
from .locking import InstanceLock
from .onboarding import Binding, scan_binding
from .paths import RuntimePaths
from .storage import save_documents


def encode_config(data: dict) -> bytes:
    """Emit the small supported TOML schema, without credentials."""
    lines = []
    def table(values, name):
        scalars = {key: value for key, value in values.items() if value is not None and not isinstance(value, dict)}
        if scalars:
            lines.append(f"[{name}]")
            for key, value in scalars.items():
                if type(value) is bool:
                    text = "true" if value else "false"
                elif isinstance(value, str):
                    text = json.dumps(value, ensure_ascii=False)
                else:
                    text = json.dumps(value, allow_nan=False)
                lines.append(f"{key} = {text}")
            lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, f"{name}.{key}")
    for name, value in data.items():
        table(value, name)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    parse_config(tomllib.loads(payload.decode("utf-8")))
    return payload


def commit_config(paths: RuntimePaths, data: dict, credentials: dict | None = None) -> None:
    payload = encode_config(data)
    documents = {}
    if credentials is not None:
        documents[paths.credentials] = CredentialStore.encode(credentials)
    documents[paths.config] = payload
    save_documents(documents, paths.backups)


def prompt(label, default=""):
    value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def acquire_binding(existing_id="") -> Binding:
    method = prompt("登录方式：1 QQ 扫码，2 手动输入", "1")
    if method == "1":
        try:
            return asyncio.run(scan_binding())
        except ValueError as error:
            print(str(error), file=sys.stderr)
            if prompt("使用手动输入？y/n", "y").lower() != "y":
                raise
    elif method != "2":
        raise ValueError("请选择 1 或 2")
    app_id = prompt("AppID", existing_id)
    secret = getpass.getpass("AppSecret（不回显）: ")
    return Binding(app_id, secret)


def wizard(paths: RuntimePaths, *, login=False, import_config: str | None = None):
    paths.initialize()
    with InstanceLock(paths.run / "application.lock"):
        # Even explicit configuration doesn't silently replace damaged files.
        existing = load_config(paths.config) if paths.config.exists() else None
        store = CredentialStore(paths)
        credentials = store.read()
        if import_config:
            source = Path(import_config).expanduser().resolve()
            payload = source.read_bytes()
            parse_config(tomllib.loads(payload.decode("utf-8")))
            save_documents({paths.config: payload}, paths.backups)
            print(f"配置已导入：{paths.config}；请执行 check-config 检查凭据。")
            return
        if not sys.stdin.isatty():
            raise ValueError("配置向导需要交互终端，请在 screen 或服务器终端运行 configure。")
        if login:
            if existing is None or existing.adapter.type != "qqofficial_websocket":
                raise ValueError("login 需要已有 QQ 官 Bot 配置；请先运行 configure。")
            data = asdict(existing)
            binding = acquire_binding(existing.adapter.qqofficial.app_id)
            data["adapter"]["qqofficial"]["app_id"] = binding.app_id
        else:
            data = asdict(existing) if existing else {
                "bot": {"name": "Tiffany"},
                "adapter": {"type": "onebot_websocket", "platform": "napcat", "websocket": {}},
            }
            data["bot"]["name"] = prompt("Bot 名称", data["bot"]["name"])
            default = "2" if existing and existing.adapter.type == "qqofficial_websocket" else "1"
            choice = prompt("接入方式：1 OneBot/NapCat，2 QQ 官 Bot", default)
            binding = None
            if choice == "1":
                previous = asdict(existing.adapter.websocket) if existing and existing.adapter.websocket else {}
                platform = existing.adapter.platform if existing and existing.adapter.websocket else "napcat"
                previous["host"] = prompt("OneBot 监听地址", previous.get("host", "127.0.0.1"))
                previous["port"] = int(prompt("OneBot 监听端口", str(previous.get("port", 6199))))
                data["adapter"] = {"type": "onebot_websocket", "platform": platform, "websocket": previous}
                token = getpass.getpass("入站 Token（不回显；留空保留已有 Token，本机可不设）: ")
                if token:
                    credentials["onebot"][platform] = {"token": token}
                parsed = parse_config(data)
                parsed.validate_credentials(lambda kind, identity: credentials[kind].get(identity, {}).get("token"))
            elif choice == "2":
                previous = asdict(existing.adapter.qqofficial) if existing and existing.adapter.qqofficial else {}
                reuse = bool(previous and store.resolve("qqofficial", previous["app_id"]))
                if reuse and prompt("保留已有 QQ 凭据？y/n", "y").lower() == "y":
                    data["adapter"] = {"type": "qqofficial_websocket", "platform": "qq_official", "qqofficial": previous}
                else:
                    binding = acquire_binding(previous.get("app_id", ""))
                    previous["app_id"] = binding.app_id
                    data["adapter"] = {"type": "qqofficial_websocket", "platform": "qq_official", "qqofficial": previous}
            else:
                raise ValueError("请选择 1 或 2")
        if binding is not None:
            credentials["qqofficial"][binding.app_id] = {
                "app_secret": binding.app_secret, "user_openid": binding.user_openid,
            }
        parsed = parse_config(data)
        parsed.validate_credentials(lambda kind, identity: credentials[kind].get(identity, {}).get(
            "app_secret" if kind == "qqofficial" else "token"))
        # Expand new defaults only during an explicit save or first setup.
        commit_config(paths, asdict(parsed), credentials)
        print(f"配置已保存：{paths.config}；凭据保存在 secrets/，未输出密钥。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--import-config")
    args = parser.parse_args()
    try:
        wizard(RuntimePaths.resolve(args.home), login=args.login, import_config=args.import_config)
        return 0
    except KeyboardInterrupt:
        print("配置已取消，未提交新配置。", file=sys.stderr)
        return 0
    except (ValueError, OSError) as error:
        print(f"配置失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
