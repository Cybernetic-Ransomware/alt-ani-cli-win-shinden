from typing import Literal

from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player import mpv, vlc


def build_command(
    kind: Literal["mpv", "vlc"],
    stream: Stream,
    *,
    title: str,
    no_detach: bool = False,
) -> list[str]:
    if kind == "vlc":
        return vlc.build(stream, title=title)
    return mpv.build(stream, title=title, no_detach=no_detach)
