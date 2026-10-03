from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    """OS-held lock; retained lock files are safe after crashes."""
    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = self.path.open("a+b")
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b" ")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            raise ValueError(f"已有实例占用运行目录（{self.path.name}），请先停止它。") from None
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()).encode("ascii"))
        stream.flush()
        self._stream = stream
        return self

    def __exit__(self, *exc):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
