from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import RuntimePaths
from .storage import save_documents


class CredentialStore:
    def __init__(self, paths: RuntimePaths):
        self.paths = paths

    def read(self) -> dict:
        path = self.paths.credentials
        if not path.exists():
            return {"version": 1, "qqofficial": {}, "onebot": {}}
        if os.name == "posix":
            path.chmod(0o600)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("qqofficial"), dict)
                    or not isinstance(data.get("onebot"), dict)):
                raise ValueError
            for kind, entries in (("qqofficial", data["qqofficial"]), ("onebot", data["onebot"])):
                for identity, entry in entries.items():
                    field = "app_secret" if kind == "qqofficial" else "token"
                    if (not isinstance(identity, str) or not identity
                            or not isinstance(entry, dict)
                            or not isinstance(entry.get(field), str) or not entry[field]):
                        raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise ValueError(f"凭据文件损坏，请修复或恢复备份：{path}") from None

    def resolve(self, kind: str, identity: str) -> str | None:
        data = self.read()
        field = "app_secret" if kind == "qqofficial" else "token"
        return data[kind].get(identity, {}).get(field)

    def secrets(self) -> tuple[str, ...]:
        data = self.read()
        return tuple(entry[field] for kind, field in (("qqofficial", "app_secret"), ("onebot", "token"))
                     for entry in data[kind].values())

    @staticmethod
    def encode(data: dict) -> bytes:
        return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def save(self, data: dict) -> None:
        save_documents({self.paths.credentials: self.encode(data)}, self.paths.backups)
