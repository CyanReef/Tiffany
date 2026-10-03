"""Private, backed up, atomic writes; reads never rewrite user files."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time


def _stage(path: Path, payload: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            temporary.chmod(0o600)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def save_documents(documents: dict[Path, bytes], backups: Path) -> None:
    """Stage all writes before replacing. Roll back on a partial write failure.

    Each file replacement is atomic. Credentials keep previous AppID entries,
    so a power loss between replacements also leaves the previous config usable.
    """
    originals = {path: path.read_bytes() if path.exists() else None for path in documents}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        backups.chmod(0o700)
    try:
        for path, payload in documents.items():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staged[path] = _stage(path, payload)
        for path, payload in originals.items():
            if payload is not None:
                destination = backups / f"{path.name}.{time.time_ns()}.bak"
                temporary = _stage(destination, payload)
                try:
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        _sync_directory(backups)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
            _sync_directory(path.parent)
    except BaseException:
        for path in reversed(replaced):
            previous = originals[path]
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                temporary = _stage(path, previous)
                try:
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for path in documents:
            candidates = sorted(backups.glob(f"{path.name}.*.bak"), reverse=True)
            for old in candidates[10:]:
                old.unlink()
