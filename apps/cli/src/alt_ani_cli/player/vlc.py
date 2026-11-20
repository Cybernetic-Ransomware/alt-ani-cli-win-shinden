import os
import shutil
import sys
from collections.abc import Mapping

from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.player.headers import get_header
from alt_ani_core.config import USER_AGENT
from alt_ani_core.errors import PlayerNotFoundError
from alt_ani_core.models import StreamVariant

# VLC 3.0 documents only --http-user-agent and --http-referrer; it has no option for arbitrary headers.
_SUPPORTED_HEADERS = {"user-agent", "referer"}


def unsupported_headers(headers: Mapping[str, str]) -> list[str]:
    return [name for name in headers if name.lower() not in _SUPPORTED_HEADERS]


def build(stream: StreamVariant, *, title: str) -> list[str]:
    path = _find()
    referer = get_header(stream.headers, "Referer")
    cmd = [
        path,
        stream.url,
        "--play-and-exit",
        f"--meta-title={title}",
        f"--http-user-agent={get_header(stream.headers, 'User-Agent') or USER_AGENT}",
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
    raise PlayerNotFoundError(EXCEPTIONS["player"]["vlc_not_found"])
