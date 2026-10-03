import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv

from deployment.paths import PROJECT_ROOT, RuntimePaths


@unittest.skipUnless(os.name == "nt", "Windows entry points")
class WindowsEntryPointTests(unittest.TestCase):
    def invoke(self, entry, args, *, cwd, env):
        if entry == "start.bat":
            line = subprocess.list2cmdline([str(PROJECT_ROOT / entry), *args])
            command = f'"{os.environ["COMSPEC"]}" /d /s /c "{line}"'
        else:
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            command = [str(powershell), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", str(PROJECT_ROOT / entry), *args]
        return subprocess.run(command, cwd=cwd, env=env, input="",
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

    def test_configuration_check_forwards_paths_and_preserves_files_without_network(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany Windows ") as temporary:
            for entry in ("start.bat", "start.ps1"):
                with self.subTest(entry=entry):
                    paths = RuntimePaths.resolve(Path(temporary) / f"data for {entry}")
                    paths.initialize()
                    content = (PROJECT_ROOT / "examples/Tiffany.onebot.toml").read_bytes()
                    paths.config.write_bytes(content)
                    environment = dict(os.environ, TIFFANY_PYTHON=sys.executable, PIP_NO_INDEX="1")
                    result = self.invoke(entry, ["check-config", "--home", str(paths.home)],
                                         cwd=temporary, env=environment)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(paths.config.read_bytes(), content)
                    self.assertEqual(list(paths.envs.iterdir()), [])
                    self.assertTrue((paths.logs / "launcher.log").exists())
                    # Automatic discovery also works when TIFFANY_PYTHON is unset.
                    environment.pop("TIFFANY_PYTHON")
                    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
                    result = self.invoke(entry, ["check-config", "--home", str(paths.home)],
                                         cwd=temporary, env=environment)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_config_reports_windows_command_without_installing(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany Windows ") as temporary:
            for entry in ("start.bat", "start.ps1"):
                with self.subTest(entry=entry):
                    paths = RuntimePaths.resolve(Path(temporary) / entry)
                    environment = dict(os.environ, TIFFANY_PYTHON=sys.executable, PIP_NO_INDEX="1")
                    result = self.invoke(entry, ["--home", str(paths.home)], cwd=temporary, env=environment)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("start.bat configure", result.stderr)
                    self.assertFalse(paths.config.exists())
                    self.assertEqual(list(paths.envs.iterdir()), [])

    def test_invalid_explicit_python_does_not_fall_back(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany Windows ") as temporary:
            environment = dict(os.environ, TIFFANY_PYTHON=str(Path(temporary) / "missing python.exe"))
            for entry in ("start.bat", "start.ps1"):
                with self.subTest(entry=entry):
                    result = self.invoke(entry, ["--help"], cwd=temporary, env=environment)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("Python 3.11+", result.stderr)
                    self.assertIn("TIFFANY_PYTHON could not be resolved", result.stderr)

    def test_windowsapps_interpreter_and_later_path_candidates_are_checked(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany Windows ") as temporary:
            root = Path(temporary)
            alias = root / "Microsoft/WindowsApps/Python"
            venv.EnvBuilder(with_pip=False).create(alias)
            shadow = root / "unavailable python"
            shadow.mkdir()
            (shadow / "python.cmd").write_bytes(b"@echo off\r\necho Python runtime unavailable 1>&2\r\nexit /b 9\r\n")
            environment = dict(os.environ)
            environment.pop("TIFFANY_PYTHON", None)
            environment.pop("PYTHONHOME", None)
            system = Path(os.environ["SystemRoot"]) / "System32"
            for with_shadow in (False, True):
                locations = [alias / "Scripts", system]
                if with_shadow:
                    locations.insert(0, shadow)
                environment["PATH"] = os.pathsep.join(str(path) for path in locations)
                for entry in ("start.bat", "start.ps1"):
                    with self.subTest(entry=entry, with_shadow=with_shadow):
                        result = self.invoke(entry, ["--help"], cwd=temporary, env=environment)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("Tiffany server launcher", result.stdout)

    def test_failed_interpreter_probe_reports_actual_reason(self):
        with tempfile.TemporaryDirectory(prefix="Tiffany Windows ") as temporary:
            shim = Path(temporary) / "python without venv.cmd"
            shim.write_bytes(b"@echo off\r\necho ModuleNotFoundError: No module named 'venv' 1>&2\r\nexit /b 1\r\n")
            environment = dict(os.environ, TIFFANY_PYTHON=str(shim))
            for entry in ("start.bat", "start.ps1"):
                with self.subTest(entry=entry):
                    result = self.invoke(entry, ["--help"], cwd=temporary, env=environment)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("No module named 'venv'", result.stderr)
                    self.assertIn(str(shim), result.stderr)
