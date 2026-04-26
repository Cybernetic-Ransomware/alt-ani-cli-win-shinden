import os
import shutil

from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.errors import PlayerNotFoundError
from alt_ani_cli.extract.common import Stream


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
    env = os.environ.get("ANI_CLI_PLAYER", "")
    if env:
        return env
    for candidate in ("mpv.exe", "mpv", "mpv.com", "mpvnet.exe", "mpvnet"):
        found = shutil.which(candidate)
        if found:
            return found
    raise PlayerNotFoundError(
        "mpv not found on PATH.\n"
        "Install via:  winget install mpv.net\n"
        "Or set env:   ANI_CLI_PLAYER=C:\\path\\to\\mpvnet.exe"
    )
