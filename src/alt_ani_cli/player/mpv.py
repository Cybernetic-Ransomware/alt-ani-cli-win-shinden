import os
import shutil
import sys
from pathlib import Path

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.errors import PlayerNotFoundError
from alt_ani_cli.extract.common import Stream

_WIN_SEARCH_PATHS: list[Path] = []
if sys.platform == "win32":
    _env = os.environ
    _appdata  = Path(_env.get("LOCALAPPDATA", ""))
    _progfiles = Path(_env.get("PROGRAMFILES", ""))
    _progfiles86 = Path(_env.get("PROGRAMFILES(X86)", ""))
    _scoop_home = Path(_env.get("SCOOP", Path.home() / "scoop"))
    _WIN_SEARCH_PATHS = [
        _appdata  / "Programs" / "mpv.net" / "mpvnet.exe",
        _appdata  / "Programs" / "mpv"     / "mpv.exe",
        _progfiles  / "mpv.net" / "mpvnet.exe",
        _progfiles  / "mpv"     / "mpv.exe",
        _progfiles86 / "mpv.net" / "mpvnet.exe",
        _scoop_home / "shims" / "mpv.exe",
        _scoop_home / "shims" / "mpvnet.exe",
    ]


def build(stream: Stream, *, title: str, no_detach: bool = False) -> list[str]:
    path = _find()
    cmd = [
        path,
        stream.url,
        f"--force-media-title={title}",
        f"--user-agent={stream.headers.get('User-Agent', USER_AGENT)}",
    ]
    referer = stream.headers.get("Referer") or stream.headers.get("referer")
    if referer:
        cmd.append(f"--referrer={referer}")
    if not no_detach:
        cmd.append("--no-terminal")
    return cmd


def _find() -> str:
    env_path = os.environ.get("ANI_CLI_PLAYER", "")
    if env_path:
        return env_path
    for candidate in ("mpv.exe", "mpv", "mpv.com", "mpvnet.exe", "mpvnet"):
        found = shutil.which(candidate)
        if found:
            return found
    for p in _WIN_SEARCH_PATHS:
        if p.is_file():
            return str(p)
    raise PlayerNotFoundError(
        "mpv / mpv.net nie znaleziony.\n"
        "Zainstaluj przez:  winget install mpv.net\n"
        "  lub:             winget install mpv\n"
        "  lub:             scoop install mpv\n"
        "Możesz też wskazać ścieżkę ręcznie:\n"
        "  $env:ANI_CLI_PLAYER = 'C:\\path\\to\\mpvnet.exe'"
    )
