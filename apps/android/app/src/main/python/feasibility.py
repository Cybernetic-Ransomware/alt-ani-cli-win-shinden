"""On-device checks for the feasibility experiment; every check reports ok/failed/skipped, none aborts the run."""

import json
import platform
import sys
import traceback
from collections.abc import Callable
from pathlib import Path


def _check(name: str, body: Callable[[], object], report: list[dict]) -> None:
    try:
        report.append({"check": name, "status": "ok", "detail": str(body())})
    except ImportError as exc:
        report.append({"check": name, "status": "skipped", "detail": f"missing module: {exc.name}"})
    except Exception as exc:
        report.append({"check": name, "status": "failed", "detail": f"{type(exc).__name__}: {traceback.format_exc(limit=2)}"})


def _engine_imports() -> str:
    import alt_ani_core.services.engine  # noqa: F401
    from alt_ani_bridge import Bridge  # noqa: F401

    return "engine and bridge import"


def _sqlite(files_dir: str) -> str:
    from alt_ani_core.history.sqlite import SqliteHistory
    from alt_ani_core.models import SeriesRef

    store = SqliteHistory(Path(files_dir) / "feasibility-history.db")
    store.record_watch(SeriesRef(id="1", slug="s", title="t", url="u"), 1.0)
    return f"{len(store.watch_entries())} watch entry"


def _dom() -> str:
    from alt_ani_core import dom
    from alt_ani_core.dom import stdlib_backend

    # selectolax has no Android wheel; the app installs the stdlib backend the same way
    dom.set_backend(stdlib_backend.parse)

    node = dom.parse("<div class='a'><iframe src='//x/y'></iframe></div>").css_first("iframe")
    return f"iframe src={node.attributes.get('src') if node else None}"


def _bridge_info() -> str:
    from alt_ani_bridge import Bridge

    reply = json.loads(Bridge().call("bridge_info", "{}"))
    if not reply["ok"]:
        raise RuntimeError(reply["error"]["code"])
    info = reply["data"]
    return f"contract {info['contract']}, {len(info['methods'])} methods, capabilities {info['capabilities']}"


def _bridge_engine(files_dir: str, cache_dir: str) -> str:
    from alt_ani_bridge import Bridge

    bridge = Bridge()
    try:
        created = json.loads(
            bridge.create_engine(
                json.dumps({"cache_dir": cache_dir, "history_path": str(Path(files_dir) / "feasibility-state.db")})
            )
        )
        if not created["ok"]:
            raise RuntimeError(created["error"]["code"])
        engine_id = created["data"]["engine_id"]
        status = json.loads(bridge.call("engine_status", json.dumps({"engine_id": engine_id})))
        if not status["ok"]:
            raise RuntimeError(status["error"]["code"])
        return f"engine created, status {status['data']}"
    finally:
        closed = json.loads(bridge.shutdown(5))
        if not (closed["ok"] and closed["data"]["ended"]):
            raise RuntimeError(f"shutdown did not end: {closed}")


def _curl_transport() -> str:
    from alt_ani_core.transport.api import EXTRACTOR_PROFILE
    from alt_ani_core.transport.curl import CurlTransport

    with CurlTransport().open(EXTRACTOR_PROFILE) as session:
        return f"GET https://example.com -> {session.get('https://example.com').status_code}"


def _ytdlp() -> str:
    from yt_dlp.version import __version__

    return f"yt-dlp {__version__}"


def run(files_dir: str, cache_dir: str) -> str:
    report: list[dict] = [
        {"check": "runtime", "status": "ok", "detail": f"{sys.version} / {platform.machine()}"},
    ]
    _check("engine_imports", _engine_imports, report)
    _check("sqlite_history", lambda: _sqlite(files_dir), report)
    _check("dom_backend", _dom, report)
    _check("bridge_info", _bridge_info, report)
    _check("bridge_engine", lambda: _bridge_engine(files_dir, cache_dir), report)
    _check("curl_transport", _curl_transport, report)
    _check("yt_dlp", _ytdlp, report)
    text = json.dumps(report, indent=2)
    # Chaquopy sends stdout to logcat (tag python.stdout), where adb can read the report
    print(f"FEASIBILITY_REPORT {json.dumps(report)}")
    return text
