import shutil
from pathlib import Path

from alt_ani_cli.config import DOWNLOADS, USER_AGENT
from alt_ani_cli.content import CONTENT
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EpisodeRow, SeriesRef
from alt_ani_cli.ui import progress

_SUPPRESS_PREFIXES = (
    "[generic] Extracting URL:",
    "[redirect] Following redirect to",
    "[info] ",
)

_LEGACY_PART_SUFFIX = ".legacy-ffmpeg"


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


def _output_template(stream: Stream, base: Path) -> str:
    # Generic extraction names extensionless direct URLs "unknown_video"; HLS keeps yt-dlp's ext since it remuxes to mp4.
    ext = "%(ext)s" if stream.ext == "m3u8" else stream.ext
    return f"{base}.{ext}"


def _quarantine_legacy_hls_part(final_path: Path) -> Path | None:
    """Move aside a .part left by the old external-ffmpeg downloader, which HlsFD would otherwise append to."""
    part = final_path.with_name(f"{final_path.name}.part")
    if not part.is_file() or final_path.with_name(f"{final_path.name}.ytdl").exists():
        return None
    backup = part.with_name(f"{part.name}{_LEGACY_PART_SUFFIX}")
    n = 1
    while backup.exists():
        backup = part.with_name(f"{part.name}{_LEGACY_PART_SUFFIX}.{n}")
        n += 1
    part.rename(backup)
    return backup


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

    opts: dict = {
        "outtmpl": _output_template(stream, dest_dir / f"{safe_title} - ep{ep_label}"),
        "http_headers": {
            **stream.headers,
            "User-Agent": stream.headers.get("User-Agent", USER_AGENT),
        },
        # The Python API defaults to zero retries and silently skips failed HLS fragments, unlike the yt-dlp CLI.
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "quiet": True,
        "noprogress": True,
        "logger": _TruncLogger(),
    }

    progress.info(CONTENT["download"]["starting"].format(title=safe_title, ep_label=ep_label, dir=dest_dir))
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(stream.url, download=False)
        final_path = Path(ydl.prepare_filename(info))
        if str(info.get("protocol", "")).startswith("m3u8") and (backup := _quarantine_legacy_hls_part(final_path)):
            progress.warn(CONTENT["download"]["legacy_part_moved"].format(path=backup))
        ydl.process_ie_result(info, download=True)

    if final_path.exists():
        progress.success(CONTENT["download"]["saved"].format(path=final_path))
