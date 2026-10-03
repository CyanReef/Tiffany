from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = tuple(sorted(set(value for value in secrets if value), key=len, reverse=True))

    def format(self, record):
        result = super().format(record)
        for secret in self.secrets:
            result = result.replace(secret, "<redacted>")
        # Also cover refreshed access tokens and Authorization header dumps.
        return re.sub(r"(?i)(Bearer |QQBot )[A-Za-z0-9._~+/=-]+", r"\1<redacted>", result)


def setup_logging(path: Path, *, level="INFO", max_bytes=10 * 1024 * 1024,
                  backups=10, secrets=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    formatter = RedactingFormatter(secrets)
    console = logging.StreamHandler()
    file = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    for handler in (console, file):
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file], force=True)
