import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deployment.configure import commit_config, wizard
from deployment.credentials import CredentialStore
from deployment.locking import InstanceLock
from deployment.logging import RedactingFormatter
from deployment.onboarding import Binding
from deployment.paths import PROJECT_ROOT, RuntimePaths
from deployment.storage import save_documents
from settings import load_config, parse_config


def onebot_config(**overrides):
    return {"bot": {"name": "test"}, "adapter": {
        "type": "onebot_websocket", "platform": "napcat", "websocket": {"port": 0, **overrides}},
        "monitoring": {"enabled": False}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = RuntimePaths.resolve(Path(self.temporary.name) / "data")
        self.paths.initialize()

    def test_path_precedence_and_default_are_independent_of_cwd(self):
        cwd = Path.cwd()
        try:
            os.chdir(self.temporary.name)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(RuntimePaths.resolve().home, PROJECT_ROOT / "data")
            with patch.dict(os.environ, {"TIFFANY_HOME": str(self.paths.home)}):
                self.assertEqual(RuntimePaths.resolve().home, self.paths.home)
                self.assertEqual(RuntimePaths.resolve(Path(self.temporary.name) / "custom").home.name, "custom")
        finally:
            os.chdir(cwd)

    def test_defaults_are_in_memory_and_startup_does_not_rewrite(self):
        commit_config(self.paths, onebot_config())
        before = self.paths.config.read_bytes(), self.paths.config.stat().st_mtime_ns
        for _ in range(3):
            config = load_config(self.paths.config)
            self.assertEqual(config.logging.max_bytes, 10 * 1024 * 1024)
            config.validate_credentials()
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.config.stat().st_mtime_ns))
        self.assertEqual(list(self.paths.backups.iterdir()), [])

    def test_corrupt_existing_config_and_credentials_remain_untouched(self):
        self.paths.config.write_bytes(b"[invalid")
        with self.assertRaises(ValueError):
            wizard(self.paths)
        self.assertEqual(self.paths.config.read_bytes(), b"[invalid")
        self.paths.credentials.write_bytes(b"invalid")
        with self.assertRaises(ValueError):
            CredentialStore(self.paths).read()
        self.assertEqual(self.paths.credentials.read_bytes(), b"invalid")

    def test_backups_retain_last_ten_and_secret_permissions(self):
        for index in range(13):
            save_documents({self.paths.credentials: str(index).encode()}, self.paths.backups)
        backups = list(self.paths.backups.glob("credentials.json.*.bak"))
        self.assertEqual(len(backups), 10)
        self.assertEqual({path.read_text() for path in backups}, {str(i) for i in range(2, 12)})
        if os.name == "posix":
            self.assertEqual(self.paths.credentials.stat().st_mode & 0o777, 0o600)
            for backup in backups:
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.paths.secrets.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.paths.backups.stat().st_mode & 0o777, 0o700)

    def test_failed_second_replacement_rolls_back_credentials_and_config(self):
        store = CredentialStore(self.paths)
        credentials = store.read()
        credentials["onebot"]["napcat"] = {"token": "original"}
        commit_config(self.paths, onebot_config(), credentials)
        original = self.paths.config.read_bytes(), self.paths.credentials.read_bytes()
        credentials["onebot"]["napcat"]["token"] = "new-secret"
        replace = os.replace
        def fail_config(source, target):
            if Path(target) == self.paths.config:
                raise OSError("simulated write failure")
            return replace(source, target)
        with patch("deployment.storage.os.replace", side_effect=fail_config):
            with self.assertRaises(OSError):
                commit_config(self.paths, onebot_config(port=1), credentials)
        self.assertEqual(original, (self.paths.config.read_bytes(), self.paths.credentials.read_bytes()))

    def test_environment_secret_precedes_local_matching_app_id(self):
        store = CredentialStore(self.paths)
        credentials = store.read()
        credentials["qqofficial"]["app-a"] = {"app_secret": "local-a", "user_openid": "openid"}
        store.save(credentials)
        config = parse_config({"adapter": {"type": "qqofficial_websocket", "platform": "qq_official",
                                          "qqofficial": {"app_id": "app-a", "app_secret_env": "CUSTOM_SECRET"}}})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.validate_credentials(store.resolve), ("local-a",))
            config.adapter.qqofficial.app_id = "app-b"
            with self.assertRaises(ValueError):
                config.validate_credentials(store.resolve)
        with patch.dict(os.environ, {"CUSTOM_SECRET": "environment-secret"}):
            self.assertEqual(config.validate_credentials(store.resolve), ("environment-secret",))
            self.assertNotIn("environment-secret", repr(config))

    def test_remote_onebot_requires_token_and_logs_redact_tracebacks(self):
        config = parse_config(onebot_config(host="0.0.0.0"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                config.validate_credentials()
            self.assertEqual(config.validate_credentials(lambda *args: "secret"), ("secret",))
        formatter = RedactingFormatter(("secret",))
        try:
            raise ValueError("secret Bearer token123 QQBot token456")
        except ValueError:
            record = logging.LogRecord("test", logging.ERROR, "", 0, "secret", (), sys.exc_info())
        output = formatter.format(record)
        for value in ("secret", "token123", "token456"):
            self.assertNotIn(value, output)

    def run_launcher(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(PROJECT_ROOT / "launcher.py"), *args,
                               "--home", str(self.paths.home)], cwd=self.temporary.name,
                              capture_output=True, text=True, encoding="utf-8", timeout=10, env=env)

    def test_check_config_works_from_other_directory_without_environment(self):
        commit_config(self.paths, onebot_config())
        result = self.run_launcher("check-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.paths.envs.iterdir()), [])

    def test_run_existing_config_prepares_environment_and_enters_supervisor(self):
        from deployment.launcher import main
        commit_config(self.paths, onebot_config())
        with patch("deployment.launcher.prepare_environment", return_value=Path(sys.executable)) as prepare, \
                patch("deployment.launcher.Supervisor.run", return_value=0) as supervise, \
                patch("deployment.launcher.setup_logging"):
            self.assertEqual(main(["--home", str(self.paths.home)]), 0)
        prepare.assert_called_once()
        self.assertIn(str(PROJECT_ROOT / "main.py"), supervise.call_args.args[0])
        self.assertEqual(supervise.call_args.kwargs["env"]["TIFFANY_HOME"], str(self.paths.home))
        self.assertEqual(supervise.call_args.kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(supervise.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_noninteractive_missing_config_reports_configure(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 2)
        self.assertIn("configure", result.stderr)
        self.assertFalse(self.paths.config.exists())

    def test_explicit_import_preserves_bytes_and_backs_up_previous(self):
        commit_config(self.paths, onebot_config())
        previous = self.paths.config.read_bytes()
        source = Path(self.temporary.name) / "old.toml"
        source.write_bytes(b'# preserve comment\n[adapter]\ntype="onebot_websocket"\nplatform="napcat"\n')
        environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
        result = self.run_launcher("configure", "--import-config", str(source), env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置已导入", result.stdout)
        self.assertEqual(self.paths.config.read_bytes(), source.read_bytes())
        self.assertEqual(next(self.paths.backups.glob("Tiffany.toml.*.bak")).read_bytes(), previous)

    def test_direct_configure_import_uses_utf8_with_legacy_output_encoding(self):
        source = Path(self.temporary.name) / "旧配置.toml"
        source.write_text('# 中文注释\n[bot]\nname="测试"\n'
                          '[adapter]\ntype="onebot_websocket"\nplatform="napcat"\n', encoding="utf-8")
        environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "deployment.configure", "--home", str(self.paths.home),
             "--import-config", str(source)], cwd=PROJECT_ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置已导入", result.stdout)
        self.assertEqual(self.paths.config.read_bytes(), source.read_bytes())
        self.assertEqual(load_config(self.paths.config).bot.name, "测试")

    def test_real_process_lock_and_crash_release(self):
        path = self.paths.run / "launcher.lock"
        with InstanceLock(path):
            result = self.run_launcher("check-config")
            self.assertEqual(result.returncode, 2)
            self.assertIn("launcher.lock", result.stderr)
        with InstanceLock(path):
            pass

    def test_qr_wizard_saves_all_fields_and_cancelled_login_keeps_bytes(self):
        import io
        from unittest.mock import AsyncMock
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["Bot", "2", "1"]), \
                patch("deployment.configure.scan_binding", AsyncMock(return_value=Binding("app", "private-binding-credential", "openid"))), \
                patch("sys.stdout", io.StringIO()) as output:
            wizard(self.paths)
        self.assertNotIn("private-binding-credential", output.getvalue())
        self.assertEqual(load_config(self.paths.config).adapter.qqofficial.app_id, "app")
        self.assertEqual(CredentialStore(self.paths).read()["qqofficial"]["app"],
                         {"app_secret": "private-binding-credential", "user_openid": "openid"})
        before = self.paths.config.read_bytes(), self.paths.credentials.read_bytes()
        with patch("sys.stdin.isatty", return_value=True), \
                patch("deployment.configure.acquire_binding", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wizard(self.paths, login=True)
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.credentials.read_bytes()))

    def test_failed_login_commit_preserves_existing_binding(self):
        credentials = CredentialStore(self.paths).read()
        credentials["qqofficial"]["old"] = {"app_secret": "old-secret", "user_openid": "old-openid"}
        data = {"adapter": {"type": "qqofficial_websocket", "platform": "qq_official", "qqofficial": {"app_id": "old"}}}
        commit_config(self.paths, data, credentials)
        before = self.paths.config.read_bytes(), self.paths.credentials.read_bytes()
        replace = os.replace
        def fail(source, target):
            if Path(target) == self.paths.config:
                raise OSError("write failed")
            return replace(source, target)
        with patch("sys.stdin.isatty", return_value=True), \
                patch("deployment.configure.acquire_binding", return_value=Binding("new", "new-secret", "new-openid")), \
                patch("deployment.storage.os.replace", side_effect=fail):
            with self.assertRaises(OSError):
                wizard(self.paths, login=True)
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.credentials.read_bytes()))
