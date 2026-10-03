from __future__ import annotations

from collections import deque
import logging
import os
import signal
import subprocess
import threading
import time

from settings import RestartConfig

logger = logging.getLogger(__name__)


class Supervisor:
    """One child process group; bounded recovery and interruptible shutdown."""
    def __init__(self, policy: RestartConfig):
        self.policy = policy
        self.child: subprocess.Popen | None = None
        self.stopping = threading.Event()
        self.stop_started: float | None = None
        self.signal_count = 0

    def request_stop(self, signum, frame=None):
        del frame
        self.signal_count += 1
        if self.stop_started is None:
            self.stop_started = time.monotonic()
        self.stopping.set()
        if self.child is not None and self.child.poll() is None:
            logger.info("请求应用 %s", "drain" if self.signal_count == 1 else "abort")
            self._signal(signum)

    def _signal(self, signum):
        child = self.child
        if child is None:
            return
        try:
            if os.name == "posix":
                os.killpg(child.pid, signum)
            elif signum == signal.SIGINT:
                child.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                child.terminate()
        except ProcessLookupError:
            pass

    def _kill_group(self):
        logger.error("应用超过 %.1f 秒停止上限，终止进程组", self.policy.stop_timeout)
        child = self.child
        if child is None:
            return
        try:
            if os.name == "posix":
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass

    def run(self, command: list[str], *, cwd=None, env=None) -> int:
        previous = {sig: signal.signal(sig, self.request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        restarts: deque[float] = deque()
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                self.child = subprocess.Popen(
                    command, cwd=cwd, env=env,
                    start_new_session=os.name == "posix",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                # A signal can arrive while Popen creates the child.
                if self.stopping.is_set():
                    self._signal(signal.SIGINT)
                while self.child.poll() is None:
                    if (self.stop_started is not None
                            and time.monotonic() - self.stop_started >= self.policy.stop_timeout):
                        self._kill_group()
                        self.child.wait()
                        return 5
                    time.sleep(0.05)
                code = self.child.returncode
                logger.info("应用退出，退出码=%s", code)
                # Drop any descendants left by a crashed application before restarting.
                if os.name == "posix":
                    try:
                        os.killpg(self.child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.child = None
                if self.stopping.is_set():
                    return code if code in (0, 2, 3, 4, 5) else 0
                if code in (0, 2, 3) or not self.policy.enabled:
                    return code
                now = time.monotonic()
                if now - started >= self.policy.window:
                    restarts.clear()
                while restarts and now - restarts[0] >= self.policy.window:
                    restarts.popleft()
                if len(restarts) >= self.policy.max_restarts:
                    logger.error("重启达到限次：%.0f 秒内 %d 次", self.policy.window, self.policy.max_restarts)
                    return 4 if code not in (4, 5) else code
                delay = self.policy.delays[min(len(restarts), len(self.policy.delays) - 1)]
                restarts.append(now)
                logger.warning("故障重启 %d/%d，等待 %.1f 秒", len(restarts), self.policy.max_restarts, delay)
                if self.stopping.wait(delay):
                    return 0
            return 0
        finally:
            if self.child is not None and self.child.poll() is None:
                self._kill_group()
                self.child.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
