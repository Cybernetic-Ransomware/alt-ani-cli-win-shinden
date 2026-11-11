from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alt_ani_core import diagnostics
from alt_ani_core.services.engine import Engine

from alt_ani_cli import main as cli_main
from alt_ani_cli import paths
from alt_ani_cli.ui import progress

from .support import STREAM, ConsoleLog, MainResult, Site


@pytest.fixture
def site(transport) -> Site:
    shinden = Site(transport)
    shinden.xhr()
    return shinden


@pytest.fixture
def console(monkeypatch) -> ConsoleLog:
    """Captures ui.progress output unwrapped, in emission order, instead of printing through Rich."""
    log = ConsoleLog()
    for level in ("info", "success", "warn", "error"):
        monkeypatch.setattr(progress, level, lambda msg, _level=level: log.messages.append((_level, str(msg))))
    return log


@pytest.fixture
def diag_events(monkeypatch):
    """Record diagnostics events as (event, fields) instead of writing the log."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(diagnostics, "_emit", lambda event, **fields: events.append((event, fields)))
    return events


@pytest.fixture
def diag_log(cli_paths) -> Callable[[], str]:
    """Content of the single session log under the isolated diagnostics directory."""

    def _read() -> str:
        (log_file,) = cli_paths.diagnostics_dir.glob("session_*.log")
        return log_file.read_text(encoding="utf-8")

    return _read


@pytest.fixture
def clean_env(monkeypatch) -> None:
    for name in (paths.ENV_XHR_BACKEND, paths.ENV_ANTIBOT_DELAY, paths.ENV_FLARESOLVERR_URL):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def actions():
    """Stubs everything past stream resolution: the extractor, the desktop player and the downloader."""
    with (
        patch("alt_ani_core.services.episode_session.extract.resolve", return_value=STREAM) as extract,
        patch("alt_ani_cli.player.runner.play", return_value=SimpleNamespace(rc=0, elapsed=5.0)) as play,
        patch("alt_ani_core.download.run", return_value=True) as download,
    ):
        yield SimpleNamespace(extract=extract, play=play, download=download)


@pytest.fixture
def run_main(transport, console, clean_env, monkeypatch) -> Callable[..., MainResult]:
    """Runs main(argv) non-interactively on the fake transport; ``code`` is None when main returned normally."""

    def _run(argv: Sequence[str], *, env: Mapping[str, str] | None = None, run: object = None) -> MainResult:
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        engines: list[Engine] = []

        def _engine(*args, **kwargs) -> Engine:
            engines.append(Engine(*args, **kwargs))
            return engines[-1]

        with (
            patch.object(cli_main, "CurlTransport", return_value=transport),
            patch.object(cli_main, "Engine", side_effect=_engine),
            patch("sys.stdin.isatty", return_value=False),
        ):
            if run is not None:
                monkeypatch.setattr(cli_main.noninteractive, "run", MagicMock(side_effect=run))
            try:
                cli_main.main(list(argv))
            except SystemExit as exc:
                return MainResult(code=exc.code if isinstance(exc.code, int) else 1, engines=engines)
        return MainResult(code=None, engines=engines)

    return _run
