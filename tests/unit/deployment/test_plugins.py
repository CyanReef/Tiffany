import asyncio
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deployment.environment import environment_location
from deployment.launcher import arguments
from deployment.plugin_commands import run_plugin_command
from deployment.plugin_environment import (
    MARKER, base_constraints, environment_identity, read_state, repair_environment,
    sync_plugins, verify_prepared, write_state,
)
from shared.paths import RuntimePaths
from shared.plugins import PluginError, PluginSettings, discover_plugins
from tests.support.plugins import plugin_project


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths.resolve(self.temp.name)
        self.paths.initialize()

    def test_default_discovery_disabled_never_imported_and_source_edits_keep_identity(self):
        root = plugin_project(self.paths.plugins / "sample", code="raise AssertionError('never import')\n")
        plan = discover_plugins(self.paths)
        self.assertEqual([s.id for s in plan.enabled], ["sample"])
        identity = plan.identity()
        (root / "plugin_sample" / "__init__.py").write_text("changed source")
        self.assertEqual(discover_plugins(self.paths).identity(), identity)
        self.paths.plugin_settings.write_bytes(PluginSettings(disabled=("sample",)).encode())
        self.assertFalse(discover_plugins(self.paths).enabled)

    def test_external_sources_resolve_against_home_and_deduplicate_real_paths(self):
        plugin_project(self.paths.home / "outside")
        self.paths.plugin_settings.write_bytes(PluginSettings(paths=("outside", str(self.paths.home / "outside"))).encode())
        plan = discover_plugins(self.paths)
        self.assertEqual(len(plan.specs), 1)
        self.assertEqual(plan.specs[0].root, self.paths.home / "outside")

    def test_duplicate_ids_and_package_names_fail(self):
        first = plugin_project(self.paths.plugins / "first")
        other = plugin_project(self.paths.plugins / "second")
        with self.assertRaisesRegex(PluginError, "duplicate"):
            discover_plugins(self.paths)
        text = (other / "pyproject.toml").read_text().replace('sample =', 'other =')
        (other / "pyproject.toml").write_text(text)
        with self.assertRaisesRegex(PluginError, "duplicate"):
            discover_plugins(self.paths)

    def test_dependencies_order_missing_disabled_cycle_and_cli_recovery(self):
        plugin_project(self.paths.plugins / "first", "first", requires=("second",))
        second = plugin_project(self.paths.plugins / "second", "second")
        self.assertEqual([s.id for s in discover_plugins(self.paths).enabled], ["second", "first"])
        self.paths.plugin_settings.write_bytes(PluginSettings(disabled=("second",)).encode())
        with self.assertRaisesRegex(PluginError, "missing or disabled"):
            discover_plugins(self.paths)
        with patch("sys.stdout", io.StringIO()):
            run_plugin_command(self.paths, "enable", "second")
        self.assertEqual(len(discover_plugins(self.paths).enabled), 2)
        plugin_project(second, "second", requires=("first",))
        with self.assertRaisesRegex(PluginError, "cycle"):
            discover_plugins(self.paths)

    def test_invalid_declarations_fail_without_loading(self):
        for change in ('api = 99', 'api = true', 'api = "1"'):
            with self.subTest(change=change):
                root = plugin_project(self.paths.plugins / "sample")
                path = root / "pyproject.toml"
                path.write_text(path.read_text().replace("api = 1", change))
                with self.assertRaises(PluginError):
                    discover_plugins(self.paths)
        for plugin_id in ("plugins", "tiffany"):
            with self.subTest(plugin_id=plugin_id):
                plugin_project(self.paths.plugins / "sample", plugin_id)
                with self.assertRaisesRegex(PluginError, "reserved"):
                    discover_plugins(self.paths)

    def test_commands_work_without_platform_config_and_do_not_rewrite_on_reads(self):
        plugin_project(self.paths.plugins / "sample")
        with patch("sys.stdout", io.StringIO()), patch("deployment.plugin_commands.prepare_environment") as prepare:
            run_plugin_command(self.paths, "list", None)
            self.assertFalse(self.paths.plugin_settings.exists())
            run_plugin_command(self.paths, "disable", "sample")
            before = self.paths.plugin_settings.read_bytes()
            run_plugin_command(self.paths, "list", None)
            self.assertEqual(before, self.paths.plugin_settings.read_bytes())
            prepare.assert_not_called()
        for argv in (("plugins", "list", "--home", self.temp.name),
                     ("--home", self.temp.name, "plugins", "sync")):
            self.assertEqual(arguments(argv).command, "plugins")


class PluginEnvironmentTests(unittest.TestCase):
    def setUp(self):
        DiscoveryTests.setUp(self)
        self.environment = self.paths.envs / "test"
        self.python = self.environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        plugin_project(self.paths.plugins / "sample")
        self.plan = discover_plugins(self.paths)

    def command(self, args, log, *, capture=False):
        return '{"plugin-sample": "0.1.0"}' if capture else ""

    def test_shared_environment_reuse_changes_and_removal(self):
        with patch("deployment.plugin_environment.run_package_command", side_effect=self.command) as run:
            sync_plugins(self.paths, self.python, self.plan)
            first = run.call_count
            self.assertEqual(read_state(self.environment)["status"], "ready")
            verify_prepared(self.paths, self.plan, environment=self.environment)
            sync_plugins(self.paths, self.python, discover_plugins(self.paths))
            self.assertEqual(run.call_count, first)
            self.paths.plugin_settings.write_bytes(PluginSettings(disabled=("sample",)).encode())
            sync_plugins(self.paths, self.python, discover_plugins(self.paths))
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any("uninstall" in cmd and "plugin-sample" in cmd for cmd in commands))
            self.assertEqual(read_state(self.environment)["managed"], [])

    def test_resolution_failure_preserves_ready_state_and_actual_failure_is_dirty(self):
        identity = environment_identity(self.python, self.plan)
        ready = {"status": "ready", "identity": identity, "managed": ["plugin-sample"]}
        write_state(self.environment, ready)
        with patch("deployment.plugin_environment.run_package_command", side_effect=ValueError("conflict")):
            with self.assertRaisesRegex(PluginError, "resolution"):
                sync_plugins(self.paths, self.python, self.plan, force=True)
        self.assertEqual(read_state(self.environment), ready)
        with patch("deployment.plugin_environment.run_package_command", side_effect=["", ValueError("interrupted")]):
            with self.assertRaisesRegex(PluginError, "installation"):
                sync_plugins(self.paths, self.python, self.plan, force=True)
        self.assertEqual(read_state(self.environment)["status"], "dirty")
        with self.assertRaisesRegex(PluginError, "previous synchronization"):
            sync_plugins(self.paths, self.python, self.plan)
        with self.assertRaises(PluginError):
            verify_prepared(self.paths, self.plan, environment=self.environment)
        with patch("deployment.plugin_environment.run_package_command", side_effect=self.command):
            sync_plugins(self.paths, self.python, self.plan, force=True)
        self.assertEqual(read_state(self.environment)["status"], "ready")

    def test_cancelled_install_remains_dirty(self):
        with patch("deployment.plugin_environment.run_package_command", side_effect=["", KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                sync_plugins(self.paths, self.python, self.plan)
        self.assertEqual(read_state(self.environment)["status"], "dirty")

    def test_corrupt_marker_blocks_startup_and_host_distribution_cannot_be_replaced(self):
        (self.environment / MARKER).write_text("{broken")
        with self.assertRaises(PluginError):
            verify_prepared(self.paths, self.plan, environment=self.environment)
        (self.environment / MARKER).unlink()
        plugin_project(self.paths.plugins / "sample", name="websockets")
        with patch("deployment.plugin_environment.run_package_command") as run:
            with self.assertRaisesRegex(PluginError, "host dependency"):
                sync_plugins(self.paths, self.python, discover_plugins(self.paths))
            run.assert_not_called()

    def test_declaration_change_during_install_never_marks_ready(self):
        def command(*args, **kwargs):
            if kwargs.get("capture"):
                plugin_project(self.paths.plugins / "second", "second")
            return self.command(*args, **kwargs)
        with patch("deployment.plugin_environment.run_package_command", side_effect=command):
            with self.assertRaisesRegex(PluginError, "changed during"):
                sync_plugins(self.paths, self.python, self.plan)
        self.assertEqual(read_state(self.environment)["status"], "dirty")

    def test_constraints_keep_markers_without_hashes(self):
        from deployment.paths import PROJECT_ROOT
        constraints = base_constraints(PROJECT_ROOT)
        self.assertIn("tiffany-hook-runtime==0.1.0", constraints)
        self.assertIn("sys_platform", constraints)
        self.assertNotIn("--hash", constraints)
        self.assertNotIn("\\", constraints)

    def test_repair_only_removes_managed_environment_and_keeps_runtime_data(self):
        environment, python, _ = environment_location(self.paths)
        environment.mkdir(parents=True)
        (environment / "broken").touch()
        protected = self.paths.storage / "business.db"
        protected.write_bytes(b"keep")
        with patch("deployment.plugin_environment.prepare_environment", return_value=python):
            self.assertEqual(repair_environment(self.paths), python)
        self.assertFalse(environment.exists())
        self.assertEqual(protected.read_bytes(), b"keep")
