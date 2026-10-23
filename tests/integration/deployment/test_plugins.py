"""Offline acceptance through real pip, editable imports, and the real Runtime."""
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import zipfile
from time import perf_counter
import unittest
from unittest.mock import patch
import venv

from deployment.plugin_environment import read_state, sync_plugins
from deployment.paths import PROJECT_ROOT
from shared.paths import RuntimePaths
from shared.plugins import PluginError, PluginSettings, discover_plugins
from tests.support.plugins import installed_wheel, plugin_project


class RealPluginEnvironmentTests(unittest.TestCase):
    def test_shipped_setuptools_template_and_host_through_real_onebot(self):
        from tools.build_release import build_release
        with tempfile.TemporaryDirectory(prefix="Tiffany shipped plugins ") as temporary:
            root = Path(temporary)
            wheels = root / "wheels"
            wheels.mkdir()
            try:
                for name in ("setuptools", "wheel", "packaging", "websockets"):
                    installed_wheel(name, wheels)
            except importlib.metadata.PackageNotFoundError as error:
                self.skipTest(f"offline build tooling not installed: {error}")
            with zipfile.ZipFile(build_release(PROJECT_ROOT, root / "release.zip")) as archive:
                archive.extractall(root)
            host = root / "Tiffany"
            paths = RuntimePaths.resolve(root / "home")
            paths.initialize()
            shutil.copytree(host / "examples/plugins/echo", paths.plugins / "echo")
            environment = paths.envs / "shipped"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            with patch.dict(os.environ, {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri()}):
                sync_plugins(paths, python, discover_plugins(paths), root=host)
                checked = subprocess.run([str(python), "-B", "-m", "deployment.plugin_worker", "check", str(paths.home)],
                                         cwd=host, capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            script = '''
import asyncio, json
from application import TiffanyApplication
from core import Bot, RuntimeState
from settings import parse_config
from shared.paths import RuntimePaths
from websockets.asyncio.client import connect
async def main():
    paths=RuntimePaths.resolve(HOME)
    bot=Bot()
    TiffanyApplication._create_bot=lambda scheduler: bot
    config=parse_config({'adapter':{'type':'onebot_websocket','platform':'napcat','websocket':{'host':'127.0.0.1','port':0}},'extensions':{'enabled':[]},'monitoring':{'enabled':False}})
    app=asyncio.create_task(TiffanyApplication._run(paths,config))
    try:
        for _ in range(300):
            if app.done(): raise AssertionError(await app)
            if bot.runtime.state == RuntimeState.RUNNING: break
            await asyncio.sleep(.01)
        adapter=bot.runtime._adapters[0]
        port=adapter._server.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as ws:
            await ws.send(json.dumps({'post_type':'message','message_type':'group','group_id':7,'user_id':1,'self_id':99,'message':'/echo hello'}))
            reply=json.loads(await asyncio.wait_for(ws.recv(),3))
            assert reply['params']['message']=='hello',reply
            await ws.send(json.dumps({'echo':reply['echo'],'status':'ok','retcode':0,'data':{'message_id':1}}))
    finally:
        await bot.stop()
    assert await app == 0
asyncio.run(main())
'''
            result = subprocess.run([str(python), "-B", "-c", "HOME=" + repr(str(paths.home)) + "\n" + script],
                                    cwd=host, capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_editable_dependencies_flat_src_conflict_removal_and_offline_reuse(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany 插件 % acceptance ") as temporary:
            root = Path(temporary)
            paths = RuntimePaths.resolve(root / "home")
            paths.initialize()
            environment = paths.envs / "real"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            # Install the actual host: isolated workers cannot borrow code from the checkout.
            from tools.build_release import build_release
            with zipfile.ZipFile(build_release(PROJECT_ROOT, root / "host.zip")) as archive:
                archive.extractall(root)
            host = root / "Tiffany"
            dependency = plugin_project(root / "dependency", "dependency", name="local-dependency")
            (dependency / "local_dependency/__init__.py").write_text("def reply(text): return 'dependency:' + text\n")
            backend_spec = importlib.util.spec_from_file_location("fixture_backend", dependency / "backend.py")
            backend = importlib.util.module_from_spec(backend_spec)
            backend_spec.loader.exec_module(backend)
            wheels = root / "wheels"
            wheels.mkdir()
            for name in ("setuptools", "wheel", "packaging", "websockets"):
                installed_wheel(name, wheels)
            backend.build_wheel(str(wheels))
            plugin_project(paths.plugins / "flat", "flat", code="def install(scope, config):\n pass\n",
                           dependencies=("tiffany-hook-runtime>=0.1,<0.2",))
            source = plugin_project(paths.plugins / "echo", "echo", src=True, requires=("flat",),
                                    dependencies=("tiffany-hook-runtime>=0.1,<0.2", "local-dependency==0.1.0"), code='''
from local_dependency import reply
from core import APP_PATHS
def install(scope, config):
    @scope.hook(on='message', uses=(APP_PATHS,))
    async def echo(ctx):
        destination = ctx.service(APP_PATHS).storage / 'reply.txt'
        destination.write_text(config['prefix'] + reply(ctx.raw['text']))
''')
            (paths.home / "config/echo.toml").write_text('prefix = "configured:"')
            plan = discover_plugins(paths)
            with patch.dict(os.environ, {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheels.as_uri()}):
                started = perf_counter()
                sync_plugins(paths, python, plan, root=host)
                cold = perf_counter() - started
                self.assertEqual(read_state(environment)["status"], "ready")
                started = perf_counter()
                with patch("deployment.plugin_environment.run_package_command", side_effect=AssertionError("warm start ran pip")):
                    sync_plugins(paths, python, discover_plugins(paths), root=host)
                warm = perf_counter() - started
                self.assertIn("local-dependency", read_state(environment)["installed"])
                # The editable path must follow subsequent ordinary source edits without pip.
                module = source / "src/plugin_echo/__init__.py"
                module.write_text(module.read_text() + "\nEDITED = True\n")
                with patch("deployment.plugin_environment.run_package_command", side_effect=AssertionError("source edit ran pip")):
                    sync_plugins(paths, python, discover_plugins(paths), root=host)
                script = '''
import asyncio
from core import APP_PATHS, Bot, Envelope
from hooks.plugins import install_plugins
from shared.paths import RuntimePaths
from shared.plugins import discover_plugins
import plugin_echo, local_dependency
assert plugin_echo.EDITED
assert 'site-packages' in local_dependency.__file__
async def exercise():
    paths=RuntimePaths.resolve(HOME)
    bot=Bot()
    bot.service(APP_PATHS,paths)
    await install_plugins(bot,paths,discover_plugins(paths))
    async with bot:
        await bot.emit(Envelope('test',{'text':'hello'},kind='message'))
    assert (paths.storage/'reply.txt').read_text() == 'configured:dependency:hello'
    assert (await bot.runtime.wait_closed()).successful
asyncio.run(exercise())
'''
                result = subprocess.run([str(python), "-B", "-c", "HOME=" + repr(str(paths.home)) + "\n" + script],
                                        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # Core constraints and all enabled plugin requirements are resolved in one operation.
                declaration = source / "pyproject.toml"
                original = declaration.read_text()
                declaration.write_text(original.replace("local-dependency==0.1.0", "local-dependency==9.0.0"))
                before = read_state(environment)
                with self.assertRaisesRegex(PluginError, "resolution"):
                    sync_plugins(paths, python, discover_plugins(paths), root=host)
                self.assertEqual(read_state(environment), before)
                declaration.write_text(original)
                # Both versions exist, but the two enabled projects cannot select different versions.
                dependency_declaration = dependency / "pyproject.toml"
                dependency_declaration.write_text(dependency_declaration.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
                backend.build_wheel(str(wheels))
                flat_declaration = paths.plugins / "flat/pyproject.toml"
                flat_original = flat_declaration.read_text()
                flat_declaration.write_text(flat_original.replace('dependencies = [', 'dependencies = ["local-dependency==0.2.0", '))
                with self.assertRaisesRegex(PluginError, "resolution"):
                    sync_plugins(paths, python, discover_plugins(paths), root=host)
                self.assertEqual(read_state(environment), before)
                flat_declaration.write_text(flat_original)
                declaration.write_text(original.replace("tiffany-hook-runtime>=0.1,<0.2", "tiffany-hook-runtime>=9"))
                with self.assertRaisesRegex(PluginError, "resolution"):
                    sync_plugins(paths, python, discover_plugins(paths), root=host)
                self.assertEqual(read_state(environment), before)
                declaration.write_text(original)
                paths.plugin_settings.write_bytes(PluginSettings(disabled=("echo",)).encode())
                sync_plugins(paths, python, discover_plugins(paths), root=host)
                installed = read_state(environment)["installed"]
                self.assertNotIn("plugin-echo", installed)
                self.assertIn("local-dependency", installed)
            print(f"\nPlugin acceptance: first sync={cold:.3f}s, offline reuse={warm * 1000:.3f}ms")
