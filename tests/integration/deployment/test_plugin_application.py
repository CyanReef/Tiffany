import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from application import TiffanyApplication
from deployment.locking import InstanceLock
from deployment.paths import PROJECT_ROOT
from deployment.plugin_commands import run_plugin_command
from shared.paths import RuntimePaths
from tests.support.plugins import plugin_project
from settings import parse_config


class PluginApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_start_failure_exits_without_retry_and_adapter_never_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.resolve(temporary)
            paths.initialize()
            source = plugin_project(paths.plugins / "fail", "fail", code='''
from core import ServiceKey
class Broken:
    async def setup(self, runtime): raise RuntimeError('plugin setup failed')
def install(scope, config): scope.service(ServiceKey('broken'), Broken())
''')
            class Adapter:
                started = False
                async def setup(self, runtime): pass
                async def start(self): self.started = True
                async def stop(self, mode): pass
                async def teardown(self): pass
            adapter = Adapter()
            config = parse_config({"adapter": {"type": "onebot_websocket", "platform": "napcat"},
                                   "extensions": {"enabled": []}, "monitoring": {"enabled": False}})
            sys.path.insert(0, str(source))
            try:
                with patch("application.verify_prepared"), patch("application.create_adapter", return_value=adapter):
                    self.assertEqual(await TiffanyApplication._run(paths, config), 2)
                self.assertFalse(adapter.started)
            finally:
                sys.path.remove(str(source))
                sys.modules.pop("plugin_fail", None)

    async def test_plugin_cancelled_initialization_is_a_terminal_startup_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.resolve(temporary)
            paths.initialize()
            source = plugin_project(paths.plugins / "cancel", "cancel", code='''
import asyncio
from core import ServiceKey
class Cancelled:
    async def setup(self, runtime): raise asyncio.CancelledError
def install(scope, config): scope.service(ServiceKey('cancel'), Cancelled())
''')
            config = parse_config({"adapter": {"type": "onebot_websocket", "platform": "napcat"},
                                   "extensions": {"enabled": []}, "monitoring": {"enabled": False}})
            sys.path.insert(0, str(source))
            try:
                with patch("application.verify_prepared"):
                    self.assertEqual(await TiffanyApplication._run(paths, config), 2)
            finally:
                sys.path.remove(str(source))
                sys.modules.pop("plugin_cancel", None)


class PluginCommandProcessTests(unittest.TestCase):
    def test_commands_from_other_directory_need_no_platform_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.resolve(temporary)
            paths.initialize()
            plugin_project(paths.plugins / "sample")
            for operation in (("list",), ("disable", "sample"), ("enable", "sample")):
                result = subprocess.run([sys.executable, "-B", str(PROJECT_ROOT / "launcher.py"),
                                         "plugins", *operation, "--home", str(paths.home)],
                                        cwd=temporary, capture_output=True, text=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(paths.config.exists())

    def test_running_application_blocks_environment_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RuntimePaths.resolve(temporary)
            paths.initialize()
            with InstanceLock(paths.run / "application.lock"), \
                 patch("deployment.plugin_commands.prepare_environment") as prepare:
                with self.assertRaisesRegex(ValueError, "已有实例"):
                    run_plugin_command(paths, "sync", None)
                prepare.assert_not_called()
