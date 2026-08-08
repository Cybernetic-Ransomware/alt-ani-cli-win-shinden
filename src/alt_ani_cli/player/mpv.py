import os
import shutil
import sys
from pathlib import Path

from alt_ani_cli.config import CACHE_DIR, USER_AGENT
from alt_ani_cli.content import EXCEPTIONS_PL
from alt_ani_cli.errors import PlayerNotFoundError
from alt_ani_cli.extract.common import Stream

# --no-detach diagnostics land here instead of relying on a console being attached —
# mpv.net (mpvnet.exe) is a GUI-subsystem app with no console-wrapper counterpart (unlike
# mpv.exe/mpv.com), so its stderr is never visible in the terminal even under --no-detach.
# A log file works regardless of which mpv frontend is installed.
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


def build(stream: Stream, *, title: str, no_detach: bool = False) -> list[str]:
    path = _find(no_detach=no_detach)
    cmd = [
        path,
        stream.url,
        f"--force-media-title={title}",
        f"--user-agent={stream.headers.get('User-Agent', USER_AGENT)}",
    ]
    referer = stream.headers.get("Referer") or stream.headers.get("referer")
    if referer:
        cmd.append(f"--referrer={referer}")
    extra_headers = {k: v for k, v in stream.headers.items() if k.lower() not in _STANDARD_HEADERS}
    if extra_headers:
        fields = ",".join(f"{k}: {v}" for k, v in extra_headers.items())
        cmd.append(f"--http-header-fields={fields}")
    if no_detach:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--log-file={LOG_FILE}")
        cmd.append("--msg-level=all=v")
    else:
        cmd.append("--no-terminal")
    return cmd


def _find(*, no_detach: bool = False) -> str:
    env_path = os.environ.get("ANI_CLI_PLAYER", "")
    if env_path:
        return env_path
    # mpv.exe is the GUI-subsystem binary — its stderr is invisible in a console even when
    # not detached. mpv.com is the console wrapper, so prefer it when the caller explicitly
    # wants to see what's happening.
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
