"""Generate immutable runtime resources in the build tree, never in the checkout."""
from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildRuntime(build_py):
    def run(self):
        super().run()
        # Import by path so build isolation does not import application dependencies.
        import runpy
        helpers = runpy.run_path(str(Path(__file__).parent / "shared/installation.py"))
        root = Path(__file__).parent.resolve()
        sources = helpers["production_sources"](root)
        for name, path in sources.items():
            if name.startswith(helpers["RESOURCE"]):
                destination = Path(self.build_lib) / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
        manifest = Path(self.build_lib) / helpers["MANIFEST"]
        manifest.write_bytes(helpers["canonical"](helpers["source_manifest"](root)))


setup(cmdclass={"build_py": BuildRuntime})
