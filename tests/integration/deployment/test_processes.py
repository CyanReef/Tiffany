"""Real subprocess coverage; POSIX signals run in the Linux CI matrix."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from deployment.configure import commit_config
from deployment.paths import PROJECT_ROOT, RuntimePaths
from tests.unit.deployment.test_storage import onebot_config


def wait_file(path, process, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"process exited early: {process.communicate()}")
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {path}")


class ApplicationProcessTests(unittest.TestCase):
    def test_abandoned_tasks_exit_child_without_asyncio_shutdown_hang(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = "\n".join([
                "import asyncio",
                "from application import TiffanyApplication",
                "from core import Bot, ServiceKey, Runtime",
                "from deployment.paths import RuntimePaths",
                "from settings import parse_config",
                "Runtime.CANCEL_GRACE=.01",
                "class Tasks:",
                " async def setup(self,runtime): self.runtime=runtime",
                " async def start(self):",
                "  async def stubborn():",
                "   while True:",
                "    try: await asyncio.sleep(.01)",
                "    except asyncio.CancelledError: pass",
                "  async def fail():",
                "   await asyncio.sleep(.05)",
                "   raise RuntimeError('critical')",
                "  self.runtime.tasks.spawn(stubborn(),name='stubborn',owner=self,critical=False)",
                "  self.runtime.tasks.spawn(fail(),name='critical',owner=self)",
                "bot=Bot()",
                "bot.service(ServiceKey('tasks'),Tasks())",
                "TiffanyApplication._create_bot=lambda scheduler=None:bot",
                f"paths=RuntimePaths.resolve({temporary!r})",
                "paths.initialize()",
                f"config=parse_config({onebot_config()!r})",
                "raise SystemExit(asyncio.run(TiffanyApplication._run(paths,config)))",
            ])
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 5, result.stderr)

    def test_application_lock_rejects_duplicate_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.resolve(temporary)
            paths.initialize()
            commit_config(paths, onebot_config())
            process = subprocess.Popen([sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--home", temporary],
                                       cwd=temporary, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                wait_file(paths.logs / "tiffany.log", process)
                result = subprocess.run([sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--home", temporary],
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


class SupervisorProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def supervisor(self, child_code, **policy):
        child = self.root / "child.py"
        child.write_text(child_code, encoding="utf-8")
        command = [sys.executable, "-B", str(child)]
        options = {"delays": [0.03], "window": 60, "max_restarts": 2, "stop_timeout": 0.25, **policy}
        code = "\n".join([
            "from deployment.supervisor import Supervisor",
            "from settings import RestartConfig",
            f"raise SystemExit(Supervisor(RestartConfig(**{options!r})).run({command!r}))",
        ])
        process = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

    def test_retryable_failures_restart_with_limit(self):
        count = self.root / "count"
        process = self.supervisor(f"from pathlib import Path\np=Path({str(count)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nraise SystemExit(4)\n")
        output = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 4, output)
        self.assertEqual(count.read_text(), "xxx")

    def test_configuration_and_permanent_auth_failures_do_not_restart(self):
        for code in (2, 3):
            with self.subTest(code=code):
                count = self.root / f"count-{code}"
                process = self.supervisor(f"from pathlib import Path\np=Path({str(count)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nraise SystemExit({code})")
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, code)
                self.assertEqual(count.read_text(), "x")

    @unittest.skipUnless(os.name == "posix", "requires Linux/POSIX signals")
    def test_signal_forwards_and_user_stop_does_not_restart(self):
        ready, stopped = self.root / "ready", self.root / "stopped"
        process = self.supervisor(
            f"import signal,time\nfrom pathlib import Path\n"
            f"def stop(sig,frame):\n Path({str(stopped)!r}).write_text(str(sig))\n raise SystemExit(0)\n"
            f"signal.signal(signal.SIGTERM,stop)\nPath({str(ready)!r}).touch()\nwhile True: time.sleep(.01)\n")
        wait_file(ready, process)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stopped.read_text(), str(signal.SIGTERM))

    @unittest.skipUnless(os.name == "posix", "requires Linux/POSIX signals")
    def test_second_signal_reaches_child(self):
        ready, stopped = self.root / "ready", self.root / "stopped"
        process = self.supervisor(
            "import signal,time\nfrom pathlib import Path\ncount=0\n"
            f"def stop(sig,frame):\n global count\n count+=1\n"
            f" Path({str(stopped)!r}).write_text(str(count))\n if count==2: raise SystemExit(0)\n"
            f"signal.signal(signal.SIGINT,stop)\nPath({str(ready)!r}).touch()\nwhile True: time.sleep(.01)\n",
            stop_timeout=2)
        wait_file(ready, process)
        process.send_signal(signal.SIGINT)
        wait_file(stopped, process)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stopped.read_text(), "2")

    @unittest.skipUnless(os.name == "posix", "requires Linux/POSIX signals")
    def test_stop_deadline_kills_stubborn_group(self):
        ready = self.root / "ready"
        process = self.supervisor(
            f"import signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"Path({str(ready)!r}).touch()\nwhile True: time.sleep(.01)\n")
        wait_file(ready, process)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 5)
        self.assertLess(time.monotonic() - started, 2)

    @unittest.skipUnless(os.name == "posix", "requires Linux/POSIX signals")
    def test_real_application_sigterm_drains_and_returns_zero(self):
        paths = RuntimePaths.resolve(self.root / "home")
        paths.initialize()
        commit_config(paths, onebot_config())
        process = subprocess.Popen([sys.executable, "-B", str(PROJECT_ROOT / "main.py"), "--home", str(paths.home)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup_process, process)
        wait_file(paths.logs / "tiffany.log", process)
        deadline = time.monotonic() + 5
        while "listening" not in (paths.logs / "tiffany.log").read_text() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        output = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("successful=True", (paths.logs / "tiffany.log").read_text())
