"""UTF-8 command output and child environments, without global env changes."""
from __future__ import annotations

import os
import sys


def configure_utf8_output() -> None:
    """Keep CLI output readable when Windows defaults to a legacy encoding."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def utf8_environment() -> dict[str, str]:
    """Explicitly configure child Python processes, preserving other variables."""
    return dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
