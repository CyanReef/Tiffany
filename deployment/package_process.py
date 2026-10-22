"""Run package operations outside the application and settle cancellation."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import threading

from .console import utf8_environment
from .paths import PROJECT_ROOT


@contextmanager
def _package_signals():
    saved = {}
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                number = getattr(signal, name)
                saved[number] = signal.signal(number, interrupt)
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


def run_package_command(command: list[str], log: Path, *, capture=False) -> str:
    """Append diagnostics to log; on interruption stop and join the process tree."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with _package_signals(), log.open("a+b") as output:
        output.seek(0, 2)
        start = output.tell()
        child = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=utf8_environment(), cwd=PROJECT_ROOT, start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            code = child.wait()
        except BaseException:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    child.kill()
                child.wait()
            if os.name == "posix":
                # The pip leader can exit before a build descendant. Revoke
                # the remaining group before the caller releases its lock.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise
        if code:
            raise ValueError(f"package operation exited {code}; diagnostics: {log}")
        if capture:
            output.seek(start)
            return output.read().decode("utf-8")
    return ""
