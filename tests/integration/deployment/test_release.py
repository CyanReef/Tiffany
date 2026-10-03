from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from deployment.paths import PROJECT_ROOT, RuntimePaths
from tools.build_release import build_release, validate_archive


class ReleaseTests(unittest.TestCase):
    def test_overlay_update_and_rollback_preserve_runtime_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "installed" / "Tiffany"
            source.mkdir()
            target.mkdir(parents=True)
            (source / "start.sh").write_text("new launcher")
            (source / "main.py").write_text("new code")
            (source / "data").mkdir()
            (source / "data" / "credential-marker").write_text("must never ship")
            (target / "main.py").write_text("old code")
            paths = RuntimePaths.resolve(target / "data")
            paths.initialize()
            protected = {paths.config: b"config", paths.credentials: b"secret",
                         paths.storage / "business.db": b"business", paths.logs / "tiffany.log": b"history\n"}
            for path, content in protected.items():
                path.write_bytes(content)
            archive = build_release(source, root / "update.zip")
            validate_archive(archive)
            with zipfile.ZipFile(archive) as bundle:
                self.assertFalse(any(name.startswith("Tiffany/data/") for name in bundle.namelist()))
                bundle.extractall(target.parent)
            self.assertEqual((target / "main.py").read_text(), "new code")
            self.assertEqual({path: path.read_bytes() for path in protected}, protected)
            (source / "main.py").write_text("old code")
            rollback = build_release(source, root / "rollback.zip")
            with zipfile.ZipFile(rollback) as bundle:
                bundle.extractall(target.parent)
            self.assertEqual((target / "main.py").read_text(), "old code")
            self.assertEqual({path: path.read_bytes() for path in protected}, protected)

    def test_real_project_release_omits_runtime_and_legacy_root_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = build_release(PROJECT_ROOT, Path(temporary) / "release.zip")
            with zipfile.ZipFile(path) as archive:
                self.assertNotIn("Tiffany/Tiffany.toml", archive.namelist())
                self.assertIn("Tiffany/requirements-server.lock", archive.namelist())
                self.assertIn("Tiffany/deployment/launcher.py", archive.namelist())
                self.assertIn("Tiffany/start.bat", archive.namelist())
                self.assertIn("Tiffany/start.ps1", archive.namelist())
                archive.extractall(temporary)
            # Import and run the shipped code from another directory, so the
            # source checkout cannot conceal missing implementation packages.
            code = """
import asyncio
from core import Bot, Envelope, RuntimePaths
from deployment.paths import RuntimePaths as DeploymentPaths

assert RuntimePaths is DeploymentPaths

async def exercise():
    bot = Bot()
    calls = []
    @bot.hook()
    async def record(ctx):
        calls.append(ctx.raw['value'])
    async with bot:
        await bot.emit(Envelope('release', {'value': 'shipped'}))
    assert calls == ['shipped']
    assert (await bot.runtime.wait_closed()).successful

asyncio.run(exercise())
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=Path(temporary) / "Tiffany", capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
