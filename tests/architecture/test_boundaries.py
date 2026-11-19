"""Layer rules of the engine/CLI split, checked on the AST so imports inside functions count too."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CORE = ROOT / "packages" / "core" / "src" / "alt_ani_core"
CLI = ROOT / "apps" / "cli" / "src" / "alt_ani_cli"
BRIDGE = ROOT / "apps" / "android" / "app" / "src" / "main" / "python" / "alt_ani_bridge"

TERMINAL_AND_DESKTOP = {
    "alt_ani_cli",
    "rich",
    "InquirerPy",
    "prompt_toolkit",
    "argparse",
    "platformdirs",
    "colorama",
    "yaml",
    "subprocess",
}

# third-party or platform-bound modules and the only engine files allowed to import them
CONFINED = {
    "curl_cffi": {"transport/curl.py"},
    "selectolax": {"dom/selectolax_backend.py"},
    "yt_dlp": {"extract/ytdlp_resolver.py", "download.py", "transfer.py"},
    "fcntl": {"history/ownership.py"},
    "msvcrt": {"history/ownership.py"},
    "sqlite3": {
        "history/database.py",
        "history/sqlite.py",
        "history/watch.py",
        "history/queue.py",
        "history/library.py",
        "history/preferences.py",
    },
    "urllib.request": {"transport/clearance.py"},
}

STDLIB_ONLY = [
    "models.py",
    "state.py",
    "errors.py",
    "events.py",
    "cancel.py",
    "redaction.py",
    "messages.py",
    "config.py",
    "rules",
]

# the engine modules the bridge may use: public services, rules, state and adapters, never a store's internals
BRIDGE_CORE_ALLOWLIST = {
    "alt_ani_core",
    "alt_ani_core.cancel",
    "alt_ani_core.config",
    "alt_ani_core.diagnostics",
    "alt_ani_core.errors",
    "alt_ani_core.events",
    "alt_ani_core.history.database",
    "alt_ani_core.history.local",
    "alt_ani_core.history.ownership",
    "alt_ani_core.history.store",
    "alt_ani_core.models",
    "alt_ani_core.redaction",
    "alt_ani_core.rules.conditions",
    "alt_ani_core.rules.criteria",
    "alt_ani_core.rules.episodes",
    "alt_ani_core.rules.job_state",
    "alt_ani_core.rules.network",
    "alt_ani_core.rules.xhr",
    "alt_ani_core.services.engine",
    "alt_ani_core.services.episode_session",
    "alt_ani_core.services.offline",
    "alt_ani_core.services.offline.events",
    "alt_ani_core.services.offline.library",
    "alt_ani_core.services.offline.sources",
    "alt_ani_core.services.playback",
    "alt_ani_core.state",
    "alt_ani_core.storage.api",
    "alt_ani_core.storage.fs",
    "alt_ani_core.transport.api",
    "alt_ani_core.transport.curl",
}
# from history.database only the error tuple; SQL stays in the stores
BRIDGE_HISTORY_NAMES = {"alt_ani_core.history.database": {"STORE_ERRORS"}}

# package → engine packages it must not import
LAYERS = {
    "rules": {
        "shinden",
        "extract",
        "services",
        "download",
        "transfer",
        "storage",
        "history",
        "dom",
        "diagnostics",
        "providers",
    },
    "transport": {"shinden", "extract", "services", "download", "history", "dom", "rules", "providers"},
    "dom": {"shinden", "extract", "services", "download", "history", "transport", "rules", "providers"},
    "providers": {"services", "download", "transfer", "history", "storage"},
    "shinden": {"extract", "services", "download", "history", "providers"},
    "extract": {"shinden", "services", "download", "history", "providers"},
    "history": {"shinden", "extract", "services", "download", "transfer", "storage", "transport", "dom"},
    "storage": {"shinden", "extract", "services", "download", "transfer", "history", "transport", "dom", "diagnostics"},
    "transfer.py": {"shinden", "extract", "services", "history", "storage", "transport", "dom"},
    "download.py": {"shinden", "extract", "services", "history", "storage", "transfer", "transport", "dom"},
    "diagnostics.py": {"shinden", "extract", "services", "download", "history", "transport", "dom"},
}


def _files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> set[str]:
    """Every dotted module name a file imports, including ``from a import b`` as ``a.b``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _uses(imports: set[str], module: str) -> bool:
    return any(name == module or name.startswith(module + ".") for name in imports)


_STD_STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}


def _aliases_of(tree: ast.AST, module: str) -> set[str]:
    """Names a module is bound to in a file (``import sys``, ``import sys as s``)."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == module
    }


def _core_targets(entry: str) -> list[Path]:
    target = CORE / entry
    return _files(target) if target.is_dir() else [target]


@pytest.mark.unit
@pytest.mark.parametrize("base", [CORE, CLI, BRIDGE], ids=["core", "cli", "bridge"])
def test_scanned_tree_is_not_empty(base):
    """A moved directory would otherwise turn every parametrized rule over it into a silent skip."""
    assert len(_files(base)) > 1


@pytest.mark.unit
class TestEngineIsPortable:
    @pytest.mark.parametrize("path", _files(CORE), ids=lambda p: p.relative_to(CORE).as_posix())
    def test_no_terminal_or_desktop_imports(self, path):
        imports = _imports(path)
        assert not {module for module in TERMINAL_AND_DESKTOP if _uses(imports, module)}

    @pytest.mark.parametrize("path", _files(CORE), ids=lambda p: p.relative_to(CORE).as_posix())
    def test_no_printing_and_no_environment_access(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences = []
        sys_names = _aliases_of(tree, "sys")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "input"}:
                offences.append(f"{node.func.id}() at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offences.append(f"os.{node.attr} at line {node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                offences += [f"os.{a.name} at line {node.lineno}" for a in node.names if a.name in {"environ", "getenv"}]
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _STD_STREAMS
                and isinstance(node.value, ast.Name)
                and node.value.id in sys_names
            ):
                offences.append(f"sys.{node.attr} at line {node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "sys":
                offences += [f"sys.{a.name} at line {node.lineno}" for a in node.names if a.name in _STD_STREAMS]
        assert not offences

    @pytest.mark.parametrize("module", sorted(CONFINED))
    def test_platform_bound_module_is_confined(self, module):
        users = {p.relative_to(CORE).as_posix() for p in _files(CORE) if _uses(_imports(p), module)}
        # non-empty, so a detector that finds nothing (e.g. paths spelled differently per OS) cannot pass vacuously
        assert users
        assert users <= CONFINED[module]

    @pytest.mark.parametrize("entry", STDLIB_ONLY)
    def test_foundation_modules_use_only_the_standard_library(self, entry):
        for path in _core_targets(entry):
            roots = {name.split(".")[0] for name in _imports(path)}
            assert roots <= set(sys.stdlib_module_names) | {"alt_ani_core"}, path

    @pytest.mark.parametrize("entry", sorted(LAYERS))
    def test_layer_does_not_reach_upwards(self, entry):
        for path in _core_targets(entry):
            imports = _imports(path)
            reached = {layer for layer in LAYERS[entry] if _uses(imports, f"alt_ani_core.{layer}")}
            assert not reached, f"{path.relative_to(CORE)} imports {reached}"

    def test_nothing_below_services_imports_services(self):
        users = {
            p.relative_to(CORE).as_posix()
            for p in _files(CORE)
            if _uses(_imports(p), "alt_ani_core.services") and "services" not in p.relative_to(CORE).parts[:-1]
        }
        assert not users

    def test_engine_imports_without_terminal_or_native_dependencies(self):
        """A fresh interpreter, because this test process has long since imported the CLI."""
        script = (
            "import sys\n"
            "import alt_ani_core.services.engine, alt_ani_core.testing, alt_ani_core.download, alt_ani_core.extract\n"
            "import alt_ani_core.history.sqlite, alt_ani_core.transport.clearance\n"
            "import alt_ani_core.storage.fs, alt_ani_core.history.ownership\n"
            "heavy = {'rich', 'InquirerPy', 'prompt_toolkit', 'platformdirs', 'yaml', 'colorama', 'curl_cffi',"
            " 'selectolax', 'yt_dlp', 'alt_ani_cli'}\n"
            "loaded = sorted(heavy & {name.split('.')[0] for name in sys.modules})\n"
            "print(','.join(loaded))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == ""


@pytest.mark.unit
class TestApplicationsStayOnTheirSide:
    @pytest.mark.parametrize("path", _files(CLI), ids=lambda p: p.relative_to(CLI).as_posix())
    def test_cli_does_not_bypass_the_engine(self, path):
        imports = _imports(path)
        assert not {module for module in ("curl_cffi", "selectolax", "yt_dlp") if _uses(imports, module)}

    def test_cli_entry_point_is_not_imported_by_other_cli_modules(self):
        users = {
            p.relative_to(CLI).as_posix()
            for p in _files(CLI)
            if _uses(_imports(p), "alt_ani_cli.main") and p.name != "__main__.py"
        }
        assert not users

    def test_cli_has_no_function_level_imports_of_its_own_modules(self):
        """The old cli <-> flow cycle was hidden behind imports inside functions."""
        offenders = []
        for path in _files(CLI):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.ImportFrom) and _uses({inner.module or ""}, "alt_ani_cli"):
                        offenders.append(f"{path.relative_to(CLI)}:{inner.lineno}")
                    if isinstance(inner, ast.Import) and _uses({a.name for a in inner.names}, "alt_ani_cli"):
                        offenders.append(f"{path.relative_to(CLI)}:{inner.lineno}")
        assert not offenders

    @pytest.mark.parametrize("path", _files(BRIDGE), ids=lambda p: p.name)
    def test_bridge_depends_on_the_engine_only(self, path):
        imports = _imports(path)
        banned = TERMINAL_AND_DESKTOP | {"selectolax", "yt_dlp"}
        assert not {module for module in banned if _uses(imports, module)}
        # the curl adapter is optional on the device and may only be loaded lazily by the default transport factory
        top_level = {
            name
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""])
        }
        assert not any("curl" in name for name in top_level)


def _is_core_module(name: str) -> bool:
    target = CORE.joinpath(*name.split(".")[1:])
    return target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()


def _engine_modules(path: Path) -> set[str]:
    """Engine modules a file imports, whether spelled ``import a.b.c``, ``from a.b import c`` or ``from a import b``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if _uses({a.name}, "alt_ani_core"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _uses({node.module or ""}, "alt_ani_core"):
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names if _is_core_module(f"{node.module}.{a.name}"))
    return found


def _from_imports(path: Path) -> list[tuple[str, str]]:
    return [
        (node.module or "", alias.name)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level == 0
        for alias in node.names
    ]


@pytest.mark.unit
class TestBridgeUsesTheEngineContract:
    @pytest.mark.parametrize("path", _files(BRIDGE), ids=lambda p: p.relative_to(BRIDGE).as_posix())
    def test_only_allowlisted_engine_modules(self, path):
        assert _engine_modules(path) - BRIDGE_CORE_ALLOWLIST == set(), "not on BRIDGE_CORE_ALLOWLIST"

    @pytest.mark.parametrize(
        "source",
        [
            "from alt_ani_core.shinden import api as shinden_api\n",
            "import alt_ani_core.shinden.api\n",
            "from alt_ani_core.shinden.api import xhr_outcome\n",
            "def f():\n    from alt_ani_core.shinden import api\n",
            "def f():\n    import alt_ani_core.shinden.api as shinden_api\n",
            "from alt_ani_core.shinden.xhr import parse_backend\n",
            "import alt_ani_core.shinden.xhr\n",
            "from alt_ani_core.shinden import xhr\n",
            "def f():\n    from alt_ani_core.shinden.xhr import parse_backend\n",
        ],
    )
    def test_provider_api_import_is_rejected(self, tmp_path, source):
        path = tmp_path / "any_bridge_module.py"
        path.write_text(source, encoding="utf-8")
        assert _engine_modules(path) - BRIDGE_CORE_ALLOWLIST

    def test_allowlist_names_no_provider_module(self):
        assert not {name for name in BRIDGE_CORE_ALLOWLIST if _uses({name}, "alt_ani_core.shinden")}

    @pytest.mark.parametrize("path", _files(BRIDGE), ids=lambda p: p.relative_to(BRIDGE).as_posix())
    def test_no_provider_module_is_imported(self, path):
        assert not _uses(_engine_modules(path), "alt_ani_core.shinden")

    def test_config_imports_without_provider_or_native_modules(self):
        script = (
            "import sys\n"
            "import alt_ani_core.config\n"
            "heavy = ('alt_ani_core.shinden', 'alt_ani_core.dom', 'alt_ani_core.providers', 'alt_ani_core.services',"
            " 'alt_ani_core.transport', 'curl_cffi', 'selectolax', 'yt_dlp', 'sqlite3')\n"
            "print(','.join(sorted(n for n in sys.modules if any(n == h or n.startswith(h + '.') for h in heavy))))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == ""

    def test_xhr_rule_imports_without_provider_or_native_modules(self):
        script = (
            "import sys\n"
            "import alt_ani_core.rules.xhr\n"
            "heavy = ('alt_ani_core.shinden', 'alt_ani_core.dom', 'alt_ani_core.providers', 'alt_ani_core.services',"
            " 'alt_ani_core.transport.curl', 'curl_cffi', 'selectolax', 'yt_dlp', 'sqlite3')\n"
            "print(','.join(sorted(n for n in sys.modules if any(n == h or n.startswith(h + '.') for h in heavy))))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == ""

    @pytest.mark.parametrize("path", _files(BRIDGE), ids=lambda p: p.relative_to(BRIDGE).as_posix())
    def test_no_sql_and_no_private_engine_names(self, path):
        assert not _uses(_imports(path), "sqlite3")
        for module, name in _from_imports(path):
            if module.startswith("alt_ani_core"):
                assert not name.startswith("_"), (module, name)
                allowed = BRIDGE_HISTORY_NAMES.get(module)
                assert allowed is None or name in allowed, (module, name)
