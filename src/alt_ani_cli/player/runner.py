import subprocess
import sys
from typing import Literal

from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player import build_command


def play(
    stream: Stream,
    *,
    kind: Literal["mpv", "vlc"],
    title: str,
    no_detach: bool = False,
) -> int:
    cmd = build_command(kind, stream, title=title, no_detach=no_detach)

    if no_detach:
        return subprocess.run(cmd).returncode

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)
    return 0
