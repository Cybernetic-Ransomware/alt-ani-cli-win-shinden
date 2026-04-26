import os
import shutil
import sys

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.errors import PlayerNotFoundError
from alt_ani_cli.extract.common import Stream


def build(stream: Stream, *, title: str) -> list[str]:
    path = _find()
    referer = stream.headers.get("Referer") or stream.headers.get("referer", "")
    cmd = [
        path,
        stream.url,
        "--play-and-exit",
        f"--meta-title={title}",
        f"--http-user-agent={stream.headers.get('User-Agent', USER_AGENT)}",
    ]
    if referer:
        cmd.append(f"--http-referrer={referer}")
    return cmd


def _find() -> str:
    env = os.environ.get("ANI_CLI_PLAYER", "")
    if env:
        return env
    candidates = ["vlc.exe", "vlc"]
    if sys.platform == "win32":
        candidates += [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ]
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
        if sys.platform == "win32" and os.path.isfile(c):
            return c
    raise PlayerNotFoundError(
        "VLC not found on PATH.\n"
        "Install via:  winget install VideoLAN.VLC\n"
        "Or set env:   ANI_CLI_PLAYER=C:\\path\\to\\vlc.exe"
    )
