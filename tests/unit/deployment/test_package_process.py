import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from deployment.package_process import run_package_command


class PackageProcessTests(unittest.TestCase):
    def test_interruption_joins_child_and_restores_signal_handlers(self):
        with tempfile.TemporaryDirectory() as temporary:
            child = Mock(pid=987654)
            child.wait.side_effect = [KeyboardInterrupt, 0]
            previous = signal.getsignal(signal.SIGTERM)
            with patch("deployment.package_process.subprocess.Popen", return_value=child), \
                 patch("deployment.package_process.subprocess.run") as kill, \
                 patch("deployment.package_process.os.killpg", create=True) as group:
                with self.assertRaises(KeyboardInterrupt):
                    run_package_command(["python", "-m", "pip"], Path(temporary) / "install.log")
                self.assertEqual(child.wait.call_count, 2)
                if os.name == "nt":
                    self.assertIn("/T", kill.call_args.args[0])
                else:
                    group.assert_any_call(child.pid, signal.SIGTERM)
                    group.assert_any_call(child.pid, signal.SIGKILL)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_nonzero_exit_reports_log_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            child = Mock()
            child.wait.return_value = 1
            log = Path(temporary) / "install.log"
            with patch("deployment.package_process.subprocess.Popen", return_value=child):
                with self.assertRaisesRegex(ValueError, "install.log"):
                    run_package_command(["python"], log)
