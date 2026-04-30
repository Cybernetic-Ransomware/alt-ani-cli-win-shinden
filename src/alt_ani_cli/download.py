import shutil
from pathlib import Path

from alt_ani_cli.config import DOWNLOADS, USER_AGENT
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.shinden.models import EpisodeRow, SeriesRef
from alt_ani_cli.ui import progress

_SUPPRESS_PREFIXES = (
    "[generic] Extracting URL:",
    "[redirect] Following redirect to",
    "[info] ",
)


class _TruncLogger:
    """yt-dlp logger that truncates long lines and suppresses noisy extraction chatter."""

    def _emit(self, msg: str) -> None:
        if any(msg.startswith(p) for p in _SUPPRESS_PREFIXES):
            return
        cols = shutil.get_terminal_size((120, 24)).columns
        if len(msg) > cols:
            msg = msg[: cols - 1] + "…"
        print(msg)

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            return
        self._emit(msg)

    def info(self, msg: str) -> None:
        self._emit(msg)

    def warning(self, msg: str) -> None:
        self._emit(f"[warn] {msg}")

    def error(self, msg: str) -> None:
        self._emit(f"[error] {msg}")


def run(
    stream: Stream,
    ep: EpisodeRow,
    series: SeriesRef,
    dest_dir: Path = DOWNLOADS,
) -> None:
    from yt_dlp import YoutubeDL

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(c for c in series.title if c.isalnum() or c in " _-").strip()
    ep_label = f"{ep.number:g}"
    out_template = str(dest_dir / f"{safe_title} - ep{ep_label}.%(ext)s")

    opts: dict = {
        "outtmpl": out_template,
        "http_headers": {
            **stream.headers,
            "User-Agent": stream.headers.get("User-Agent", USER_AGENT),
        },
        "quiet": True,
        "logger": _TruncLogger(),
    }

    # Use ffmpeg for HLS if available — avoids timestamp warnings and supports merging
    if shutil.which("ffmpeg"):
        opts["external_downloader"] = "ffmpeg"
        opts["external_downloader_args"] = {"ffmpeg_i": ["-loglevel", "warning"]}

    progress.info(f"Pobieranie: {safe_title} ep{ep_label} → {dest_dir}")
    with YoutubeDL(opts) as ydl:
        ydl.download([stream.url])

    # Find the downloaded file and report its path
    candidates = sorted(dest_dir.glob(f"{safe_title} - ep{ep_label}.*"))
    if candidates:
        progress.success(f"Zapisano: {candidates[-1]}")
