import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deployment.environment import environment_location
from deployment.launcher import main
from shared.installation import HostInstallation, source_manifest
from shared.paths import RuntimePaths
from tests.support.plugins import plugin_project
from tests.unit.deployment.test_storage import onebot_config
from deployment.configure import commit_config


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="Tiffany 安装 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = RuntimePaths.resolve(self.root / "data")
        logging = patch("deployment.launcher.setup_logging")
        logging.start()
        self.addCleanup(logging.stop)

    def test_same_version_and_lock_with_different_host_code_select_new_environment(self):
        root = plugin_project(self.root / "source", name="tiffany-hook-runtime")
        (root / "requirements-server.lock").write_bytes(b"")
        first = HostInstallation.detect(root)
        source = root / "tiffany_hook_runtime/__init__.py"
        source.write_text("CHANGED = True\n")
        second = HostInstallation.detect(root)
        self.assertEqual(first.version, second.version)
        self.assertEqual(first.lock_bytes, second.lock_bytes)
        self.assertNotEqual(first.content_id, second.content_id)
        with patch("deployment.environment.HostInstallation.detect", return_value=first):
            old = environment_location(self.paths)[0]
        with patch("deployment.environment.HostInstallation.detect", return_value=second):
            new = environment_location(self.paths)[0]
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(ValueError, "changed"):
            first.verify()

    def test_manifest_is_deterministic_without_absolute_paths(self):
        host = HostInstallation.detect()
        self.assertEqual(host.manifest, source_manifest(host.root))
        self.assertNotIn(str(host.root), repr(host.manifest))

    def test_installed_default_and_explicit_home_precedence(self):
        with patch("importlib.resources.files") as resource, patch.dict(os.environ, {}, clear=True):
            resource.return_value.joinpath.return_value.is_file.return_value = True
            self.assertEqual(RuntimePaths.resolve().home, Path.cwd() / "data")
            with patch.dict(os.environ, {"TIFFANY_HOME": str(self.root / "environment")}):
                self.assertEqual(RuntimePaths.resolve().home, self.root / "environment")
                self.assertEqual(RuntimePaths.resolve(self.paths.home).home, self.paths.home)

    def test_version_and_help_do_not_create_home_or_prepare_environment(self):
        for args in (["--version"], ["--help"]):
            with patch("deployment.launcher.prepare_environment") as prepare, patch("sys.stdout", io.StringIO()) as output:
                try:
                    code = main([*args, "--home", str(self.paths.home)])
                except SystemExit as result:
                    code = result.code
                self.assertEqual(code, 0)
                self.assertIn("Tiffany", output.getvalue())
                prepare.assert_not_called()
                self.assertFalse(self.paths.home.exists())

    def test_repeated_init_keeps_config_and_never_connects(self):
        self.paths.initialize()
        commit_config(self.paths, onebot_config())
        before = self.paths.config.read_bytes(), self.paths.config.stat().st_mtime_ns
        with patch("deployment.launcher.prepare_environment", return_value=Path(os.sys.executable)) as prepare, \
                patch("deployment.plugin_environment.sync_plugins") as sync, \
                patch("deployment.launcher.Supervisor.run") as supervise, \
                patch("deployment.launcher.subprocess.call") as wizard, patch("sys.stdout", io.StringIO()):
            for _ in range(2):
                self.assertEqual(main(["init", "--home", str(self.paths.home)]), 0)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(sync.call_count, 2)
            supervise.assert_not_called()
            wizard.assert_not_called()
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.config.stat().st_mtime_ns))

    def test_cancelled_init_does_not_save_config_or_start_application(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("deployment.launcher.prepare_environment", return_value=Path(os.sys.executable)), \
                patch("deployment.launcher.subprocess.call", return_value=0), \
                patch("deployment.launcher.Supervisor.run") as supervise, patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["init", "--home", str(self.paths.home)]), 0)
            self.assertFalse(self.paths.config.exists())
            supervise.assert_not_called()

    def test_noninteractive_missing_configuration_never_installs(self):
        for command in ("init", "run", "check-config"):
            with patch("sys.stdin.isatty", return_value=False), \
                    patch("deployment.launcher.prepare_environment") as prepare, patch("sys.stderr", io.StringIO()) as output:
                self.assertEqual(main([command, "--home", str(self.paths.home)]), 2)
                self.assertIn("tiffany init", output.getvalue())
                prepare.assert_not_called()
