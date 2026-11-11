"""Build both wheels and prove they work outside the workspace: entry points, packaged content, a terminal-free engine.

Run with ``uv run python tools/check_packaging.py``; needs network or a warm uv cache to install dependencies.
"""

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CORE_ONLY_CHECK = """
import sys, tempfile
from pathlib import Path
from alt_ani_core import dom
from alt_ani_core.config import EngineConfig
from alt_ani_core.services.engine import Engine
from alt_ani_core.testing import FakeTransport, InMemoryHistory

with Engine(EngineConfig(cache_dir=Path(tempfile.mkdtemp())), transport=FakeTransport(), history=InMemoryHistory()) as engine:
    ref = engine.series_from_url("https://shinden.pl/series/61064-sousou-no-frieren")
    assert ref.id == "61064", ref
try:
    dom.parse("<p></p>")
except RuntimeError:
    pass
else:
    raise SystemExit("the DOM backend must be an explicit extra, yet one was importable")
heavy = {"rich", "InquirerPy", "prompt_toolkit", "platformdirs", "yaml", "colorama", "curl_cffi", "selectolax", "alt_ani_cli"}
loaded = sorted(heavy & {name.split(".")[0] for name in sys.modules})
assert not loaded, loaded
print("core-only ok")
"""


def run(*cmd: str, cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def venv_script(venv: Path, name: str) -> Path:
    return venv / (f"Scripts/{name}.exe" if sys.platform == "win32" else f"bin/{name}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="alt-ani-packaging-"))
    try:
        dist = work / "dist"
        run("uv", "build", "--all-packages", "--wheel", "-o", str(dist), cwd=ROOT)
        core_wheel = next(dist.glob("alt_ani_core-*.whl"))
        cli_wheel = next(dist.glob("alt_ani_cli_win-*.whl"))

        names = zipfile.ZipFile(cli_wheel).namelist()
        for required in (
            "alt_ani_cli/content/ui_content_pl-pl.yaml",
            "alt_ani_cli/content/exceptions_content_pl-pl.yaml",
            "alt_ani_cli/content/exceptions_content_en-us.yaml",
            "alt_ani_cli/__main__.py",
        ):
            if required not in names:
                raise SystemExit(f"{required} is missing from {cli_wheel.name}")
        if any(name.startswith("alt_ani_core/") for name in names):
            raise SystemExit("the CLI wheel must not bundle the engine")

        app = work / "venv-app"
        run("uv", "venv", "--python", sys.executable, str(app))
        run("uv", "pip", "install", "--python", str(venv_python(app)), str(core_wheel) + "[curl,selectolax]", str(cli_wheel))
        version = run(str(venv_script(app, "alt-ani-cli")), "--version", cwd=work)
        module_version = run(str(venv_python(app)), "-m", "alt_ani_cli", "--version", cwd=work)
        if not version.startswith("alt-ani-cli ") or version != module_version:
            raise SystemExit(f"unexpected version output: {version!r} / {module_version!r}")
        print(f"entry points ok: {version}")

        core = work / "venv-core"
        run("uv", "venv", "--python", sys.executable, str(core))
        run("uv", "pip", "install", "--python", str(venv_python(core)), str(core_wheel))
        print(run(str(venv_python(core)), "-c", CORE_ONLY_CHECK, cwd=work))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
