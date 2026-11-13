"""alt-ani-cli — CLI client for shinden.pl, optimised for Windows PowerShell."""

import platform
import sys
from collections.abc import Sequence
from io import TextIOWrapper

from alt_ani_cli import __version__, legacy_history, noninteractive, paths
from alt_ani_cli.args import parse_args
from alt_ani_cli.content import CONTENT, error_text
from alt_ani_cli.event_renderer import ConsoleEvents
from alt_ani_cli.flow import handlers as flow
from alt_ani_cli.legacy_history import ImportStatus
from alt_ani_cli.paths import CliPaths
from alt_ani_cli.ui import progress
from alt_ani_core import diagnostics
from alt_ani_core.config import SHINDEN_BASE
from alt_ani_core.errors import (
    AntiBotError,
    DownloadFailedError,
    DownloadTargetError,
    FilterMismatchError,
    IncompleteEpisodeListError,
    NoStreamError,
    ParseError,
    PlayerNotFoundError,
    ShindenError,
    ShindenXhrError,
)
from alt_ani_core.history.sqlite import SqliteHistory
from alt_ani_core.redaction import redact_text
from alt_ani_core.services.engine import Engine
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.transport.api import HttpStatusError, TransportError
from alt_ani_core.transport.curl import CurlTransport

_PROG = CONTENT["progress"]


def _setup_encoding() -> None:
    """Reconfigure stdout/stderr to UTF-8 before first Rich Console use."""
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        import colorama

        colorama.just_fix_windows_console()


def _open_history(cli_paths: CliPaths) -> SqliteHistory:
    """The legacy import runs before anything else touches history, so -D also covers what it brought in."""
    store = SqliteHistory(cli_paths.history_db)
    # opened right away, so a database from a newer version stops the run before anything is played or downloaded
    store.meta(legacy_history.META_SHA)
    source = cli_paths.legacy_history
    report = legacy_history.import_legacy(store, source)
    if report is None:
        return store
    if report.status is ImportStatus.CORRUPT:
        progress.warn(_PROG["history_import_corrupt"].format(path=source))
    elif report.status is ImportStatus.UNAVAILABLE:
        progress.warn(_PROG["history_import_unavailable"].format(path=source))
    elif report.skipped:
        progress.warn(_PROG["history_import_skipped"].format(count=report.skipped, path=source))
    return store


def _report_http_error(exc: HttpStatusError) -> None:
    url = exc.url or ""
    progress.error(_PROG["http_error"].format(status=exc.status, url=redact_text(url)))
    if exc.status == 403 and SHINDEN_BASE in url:
        solver = paths.flaresolverr_url()
        if solver:
            progress.warn(_PROG["flaresolverr_unreachable"].format(url=redact_text(solver)))
        else:
            progress.warn(_PROG["cloudflare_hint"])


def main(argv: Sequence[str] | None = None) -> None:  # noqa: C901
    _setup_encoding()
    options = parse_args(argv)
    cli_paths = paths.load_paths()

    try:
        history = _open_history(cli_paths)
        if options.delete_history:
            history.clear()
            progress.success(_PROG["history_cleared"])
            sys.exit(0)
    except ShindenError as exc:
        progress.error(_PROG["shinden_error"].format(exc=error_text(exc)))
        sys.exit(1)

    try:
        config = paths.load_engine_config(cli_paths)
    except ValueError as exc:
        progress.error(str(exc))
        sys.exit(1)

    engine = Engine(config, transport=CurlTransport(), history=history, events=ConsoleEvents())
    interactive = sys.stdin.isatty() and not options.select_nth
    mode = "interactive" if interactive else "noninteractive"

    diag_path = diagnostics.configure(cli_paths.diagnostics_dir)
    diagnostics.session_start(__version__, platform.python_version(), platform.platform(), mode=mode)
    progress.info(_PROG["diagnostics_log_hint"].format(path=diag_path))

    try:
        if interactive:
            flow.run_interactive(options, engine, cli_paths)
        else:
            noninteractive.run(options, engine, cli_paths)
    except KeyboardInterrupt:
        diagnostics.session_end("interrupted", None)
        progress.warn(_PROG["interrupted"])
        sys.exit(130)
    except ShindenXhrError as exc:
        diagnostics.session_end("error", type(exc).__name__)
        progress.error(shinden_api.stop_message(exc))
        sys.exit(1)
    except HttpStatusError as exc:
        diagnostics.session_end("error", type(exc).__name__)
        _report_http_error(exc)
        sys.exit(1)
    except TransportError as exc:
        diagnostics.session_end("error", type(exc).__name__)
        progress.error(_PROG["network_error"].format(exc=redact_text(exc)))
        sys.exit(1)
    except (
        AntiBotError,
        NoStreamError,
        ParseError,
        IncompleteEpisodeListError,
        FilterMismatchError,
        DownloadFailedError,
        DownloadTargetError,
    ) as exc:
        diagnostics.session_end("error", type(exc).__name__)
        progress.error(error_text(exc))
        sys.exit(1)
    except PlayerNotFoundError as exc:
        diagnostics.session_end("error", type(exc).__name__)
        progress.error(str(exc))
        sys.exit(1)
    except ShindenError as exc:
        diagnostics.session_end("error", type(exc).__name__)
        progress.error(_PROG["shinden_error"].format(exc=error_text(exc)))
        sys.exit(1)
    else:
        diagnostics.session_end("ok", None)
    finally:
        engine.close()
