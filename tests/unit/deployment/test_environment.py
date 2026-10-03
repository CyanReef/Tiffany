import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deployment.environment import prepare_environment
from deployment.paths import RuntimePaths


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = RuntimePaths.resolve(self.temporary.name)
        self.paths.initialize()
        self.lock = Path(self.temporary.name) / "server.lock"
        self.lock.write_text("version-one")

    def fake_create(self, target):
        python = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("fake python")

    def test_success_reuse_is_offline_and_dependency_changes_preserve_rollback(self):
        with patch("deployment.environment.venv.EnvBuilder.create", side_effect=self.fake_create) as create:
            with patch("deployment.environment.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                first = prepare_environment(self.paths, self.lock)
                self.assertTrue((first.parent.parent / ".tiffany-ready.json").exists())
                self.assertEqual(run.call_count, 2)
                self.assertIn("--require-hashes", run.call_args_list[0].args[0])
                run.reset_mock()
                self.assertEqual(prepare_environment(self.paths, self.lock), first)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("pip", run.call_args.args[0])
                self.lock.write_text("version-two")
                second = prepare_environment(self.paths, self.lock)
                self.assertNotEqual(first, second)
                self.assertTrue(first.exists())
                self.lock.write_text("version-one")
                self.assertEqual(prepare_environment(self.paths, self.lock), first)
                self.assertEqual(create.call_count, 2)

    def test_install_failure_never_marks_environment_ready_and_retry_succeeds(self):
        with patch("deployment.environment.venv.EnvBuilder.create", side_effect=self.fake_create):
            with patch("deployment.environment.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pip")):
                with self.assertRaises(ValueError):
                    prepare_environment(self.paths, self.lock)
            self.assertEqual(list(self.paths.envs.glob("*/.tiffany-ready.json")), [])
            with patch("deployment.environment.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
                prepare_environment(self.paths, self.lock)
            self.assertEqual(len(list(self.paths.envs.glob("*/.tiffany-ready.json"))), 1)

    def test_platform_change_preserves_existing_environment_and_prepares_another(self):
        with patch("deployment.environment.venv.EnvBuilder.create", side_effect=self.fake_create) as create:
            with patch("deployment.environment.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
                with patch("deployment.environment.sysconfig.get_platform", return_value="win-amd64"):
                    windows = prepare_environment(self.paths, self.lock)
                with patch("deployment.environment.sysconfig.get_platform", return_value="linux-x86_64"):
                    linux = prepare_environment(self.paths, self.lock)
                self.assertNotEqual(windows, linux)
                self.assertTrue(windows.exists())
                self.assertTrue(linux.exists())
                self.assertEqual(create.call_count, 2)
                marker = json.loads((linux.parent.parent / ".tiffany-ready.json").read_text())
                self.assertEqual(marker["platform"], "linux-x86_64")
                with patch("deployment.environment.sysconfig.get_platform", return_value="win-amd64"):
                    self.assertEqual(prepare_environment(self.paths, self.lock), windows)
                self.assertEqual(create.call_count, 2)
