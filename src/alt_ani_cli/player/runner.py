import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Literal

from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player import build_command


@dataclass
class PlayResult:
    rc: int
    elapsed: float  # seconds; 0.0 in detached mode, where it isn't measured


def play(
    stream: Stream,
    *,
    kind: Literal["mpv", "vlc"],
    title: str,
    no_detach: bool = False,
) -> PlayResult:
    cmd = build_command(kind, stream, title=title, no_detach=no_detach)

    if no_detach:
        start = time.monotonic()
        rc = subprocess.run(cmd).returncode
        return PlayResult(rc=rc, elapsed=time.monotonic() - start)

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)
    return PlayResult(rc=0, elapsed=0.0)
