"""Local-only session diagnostics log for interactive soak testing.

Never sent over the network — written to a plain-text file under
``CACHE_DIR/diagnostics``. Every public function here accepts only
already-redacted primitives (hosts, exception class names, ids, titles,
numbers) — never a raw URL, header dict, or exception message — so redaction
is enforced by the function signatures, not by caller discipline.
"""

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from alt_ani_cli.config import CACHE_DIR

DIAG_DIR = CACHE_DIR / "diagnostics"
_MAX_SESSIONS = 20

_logger = logging.getLogger("alt_ani_cli.diagnostics")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_logger.addHandler(logging.NullHandler())


def configure() -> Path:
    """Safe to call repeatedly — closes any prior handler first, so events never duplicate across two open files."""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)

    for handler in [h for h in _logger.handlers if not isinstance(h, logging.NullHandler)]:
        _logger.removeHandler(handler)
        handler.close()

    path = DIAG_DIR / f"session_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _logger.addHandler(handler)

    _prune()
    return path


def _prune() -> None:
    """Runs after the new file exists, so the count includes it — pruning first would leave one file too many."""
    sessions = sorted(DIAG_DIR.glob("session_*.log"), key=lambda p: p.stat().st_mtime)
    excess = len(sessions) - _MAX_SESSIONS
    for old in sessions[: max(excess, 0)]:
        old.unlink(missing_ok=True)


def _host_of_url(url: str) -> str:
    """Reduce a full URL to its bare hostname — the only function in this module that touches a raw URL."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return "unknown-host"
    return host.removeprefix("www.") if host else "unknown-host"


def _safe_value(value: object) -> str:
    """Collapse whitespace/newlines and quote if needed, so one field can't corrupt the log line structure."""
    text = " ".join(str(value).split())
    return f'"{text}"' if " " in text else text


def _emit(event: str, **fields: object) -> None:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_safe_value(value)}")
    _logger.info(" ".join(parts))


def session_start(version: str, python_version: str, platform: str) -> None:
    _emit("session_start", version=version, python=python_version, platform=platform)


def session_end(outcome: str, exc: str | None) -> None:
    _emit("session_end", outcome=outcome, exc=exc)


def screen_transition(from_screen: str, to_screen: str, elapsed: float) -> None:
    _emit("screen_transition", from_screen=from_screen, to_screen=to_screen, elapsed=f"{elapsed:.3f}")


def series_selected(series_id: str, title: str) -> None:
    _emit("series_selected", id=series_id, title=title)


def episode_selected(number: float, title: str) -> None:
    _emit("episode_selected", number=number, title=title)


def player_selected(online_id: str, player: str, host: str | None) -> None:
    _emit("player_selected", online_id=online_id, player=player, host=host)


def resolve_result(host: str | None, ok: bool, exc: str | None, elapsed: float) -> None:
    _emit("resolve_result", host=host, ok=ok, exc=exc, elapsed=f"{elapsed:.3f}")


def playback_result(kind: str, rc: int, elapsed: float, confirmed: bool, mpv_log: str | None) -> None:
    _emit("playback_result", kind=kind, rc=rc, elapsed=f"{elapsed:.3f}", confirmed=confirmed, mpv_log=mpv_log)


def history_update(series_id: str, last_ep: float) -> None:
    _emit("history_update", id=series_id, last_ep=last_ep)
