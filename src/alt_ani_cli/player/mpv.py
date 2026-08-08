import os
import shutil
import sys
from pathlib import Path

from alt_ani_cli.config import CACHE_DIR, USER_AGENT
from alt_ani_cli.content import EXCEPTIONS_PL
from alt_ani_cli.errors import PlayerNotFoundError
from alt_ani_cli.extract.common import Stream

# Written on every run (not just --no-detach), since playback glitches are usually
# intermittent, and mpv.net's GUI subsystem hides stderr from the console either way.
LOG_FILE = CACHE_DIR / "mpv-debug.log"

_WIN_SEARCH_PATHS: list[Path] = []
if sys.platform == "win32":
    _env = os.environ
    _appdata = Path(_env.get("LOCALAPPDATA", ""))
    _progfiles = Path(_env.get("PROGRAMFILES", ""))
    _progfiles86 = Path(_env.get("PROGRAMFILES(X86)", ""))
    _scoop_home = Path(_env.get("SCOOP", Path.home() / "scoop"))
    _WIN_SEARCH_PATHS = [
        _appdata / "Programs" / "mpv.net" / "mpvnet.exe",
        _appdata / "Programs" / "mpv" / "mpv.exe",
        _progfiles / "mpv.net" / "mpvnet.exe",
        _progfiles / "mpv" / "mpv.exe",
        _progfiles86 / "mpv.net" / "mpvnet.exe",
        _scoop_home / "shims" / "mpv.exe",
        _scoop_home / "shims" / "mpvnet.exe",
    ]


_STANDARD_HEADERS = {"user-agent", "referer"}


def _get_header(headers: dict[str, str], name: str) -> str | None:
    lname = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lname), None)


def build(stream: Stream, *, title: str, no_detach: bool = False) -> list[str]:
    path = _find(no_detach=no_detach)
    cmd = [
        path,
        stream.url,
        f"--force-media-title={title}",
        f"--user-agent={_get_header(stream.headers, 'User-Agent') or USER_AGENT}",
    ]
    referer = _get_header(stream.headers, "Referer")
    if referer:
        cmd.append(f"--referrer={referer}")
    extra_headers = {k: v for k, v in stream.headers.items() if k.lower() not in _STANDARD_HEADERS}
    if extra_headers:
        fields = ",".join(f"{k}: {v}" for k, v in extra_headers.items())
        cmd.append(f"--http-header-fields={fields}")
    # EXPERIMENTAL: some CDNs (observed on uqload.is) reset the HLS segment connection every
    # ~10s (TLS -10054/ECONNRESET), corrupting packets faster than ffmpeg's own HLS-level
    # retry recovers from. reconnect_streamed extends libavformat's auto-reconnect to
    # non-seekable streamed sources like HLS; unverified whether it actually helps here.
    # No reconnect_delay_max override — this flag applies to every stream mpv opens, not
    # just flaky ones, so keep ffmpeg's own default (120s) rather than capping it low.
    cmd.append("--stream-lavf-o=reconnect=1,reconnect_streamed=1")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cmd.append(f"--log-file={LOG_FILE}")
    cmd.append("--msg-level=all=v")
    if not no_detach:
        cmd.append("--no-terminal")
    return cmd


def _find(*, no_detach: bool = False) -> str:
    env_path = os.environ.get("ANI_CLI_PLAYER", "")
    if env_path:
        return env_path
    # mpv.exe's stderr never reaches a console; mpv.com is the console wrapper, so prefer
    # it when the caller wants to see what's happening.
    order = (
        ("mpv.com", "mpv.exe", "mpv", "mpvnet.exe", "mpvnet")
        if no_detach
        else ("mpv.exe", "mpv", "mpv.com", "mpvnet.exe", "mpvnet")
    )
    for candidate in order:
        found = shutil.which(candidate)
        if found:
            return found
    for p in _WIN_SEARCH_PATHS:
        if p.is_file():
            return str(p)
    raise PlayerNotFoundError(EXCEPTIONS_PL["player"]["mpv_not_found"])
