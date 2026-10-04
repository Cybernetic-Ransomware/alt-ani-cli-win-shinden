import gc
import os
import shutil
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from alt_ani_cli.config import DOWNLOADS, USER_AGENT
from alt_ani_cli.content import CONTENT, EXCEPTIONS_PL
from alt_ani_cli.errors import DownloadFailedError, DownloadTargetError
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EpisodeRow, SeriesRef
from alt_ani_cli.ui import progress

_SUPPRESS_PREFIXES = (
    "[generic] Extracting URL:",
    "[redirect] Following redirect to",
    "[info] ",
)

_LEGACY_PART_SUFFIX = ".legacy-ffmpeg"
_STAGING_INFIX = ".redownload"


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


def _staging_path(final_path: Path) -> Path:
    return final_path.with_name(f"{final_path.stem}{_STAGING_INFIX}{final_path.suffix}")


def _staging_artifacts(final_path: Path) -> list[Path]:
    prefix = f"{final_path.stem}{_STAGING_INFIX}."
    return [path for path in final_path.parent.iterdir() if path.name.startswith(prefix)]


def _discard_staging(final_path: Path) -> list[Path]:
    """Remove every artifact of a redownload attempt (staged file, .part/.ytdl, fragments, fixup temp); return leftovers."""
    for path in _staging_artifacts(final_path):
        if path.is_file():
            with suppress(OSError):
                path.unlink()
    return _staging_artifacts(final_path)


def _redirect_to_staging(ydl, info: dict, final_path: Path) -> Path:
    """Point yt-dlp at a sibling staging file so the existing final stays intact until the new copy is complete."""
    # A staging partial may come from another source or a crashed run; resuming or promoting it could splice stale data.
    if leftovers := _discard_staging(final_path):
        raise DownloadTargetError(EXCEPTIONS_PL["download"]["staging_locked"].format(path=leftovers[0]))
    staging = _staging_path(final_path)
    ydl.params["outtmpl"]["default"] = str(staging).replace("%", "%%")
    if Path(ydl.prepare_filename(info)) != staging:
        raise DownloadTargetError(EXCEPTIONS_PL["download"]["staging_mismatch"].format(path=final_path))
    return staging


def _promote_staging(staging: Path, final_path: Path) -> None:
    try:
        os.replace(staging, final_path)
    except OSError:
        _discard_staging(final_path)
        raise DownloadTargetError(EXCEPTIONS_PL["download"]["replace_failed"].format(path=final_path)) from None


def run(
    stream: Stream,
    ep: EpisodeRow,
    series: SeriesRef,
    dest_dir: Path = DOWNLOADS,
    *,
    confirm_overwrite: Callable[[Path], bool] | None = None,
) -> bool:
    """Returns False when an existing final was kept; without confirm_overwrite an existing final is never replaced."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(c for c in series.title if c.isalnum() or c in " _-").strip()
    ep_label = f"{ep.number:g}"

    opts: dict = {
        "outtmpl": _output_template(stream, dest_dir / f"{safe_title} - ep{ep_label}"),
        "http_headers": {
            **stream.headers,
            "User-Agent": stream.headers.get("User-Agent", USER_AGENT),
        },
        # Match yt-dlp CLI retry counts, but fail instead of silently skipping unavailable HLS fragments.
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "quiet": True,
        "noprogress": True,
        "logger": _TruncLogger(),
    }

    progress.info(CONTENT["download"]["starting"].format(title=safe_title, ep_label=ep_label, dir=dest_dir))
    failed = False
    staging: Path | None = None
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(stream.url, download=False)
            final_path = Path(ydl.prepare_filename(info))
            if confirm_overwrite is not None and final_path.exists():
                if not confirm_overwrite(final_path):
                    return False
                staging = _redirect_to_staging(ydl, info, final_path)
            target = staging or final_path
            if str(info.get("protocol", "")).startswith("m3u8") and (backup := _quarantine_legacy_hls_part(target)):
                progress.warn(CONTENT["download"]["legacy_part_moved"].format(path=backup))
            ydl.process_ie_result(info, download=True)
    except DownloadError:
        failed = True

    if failed:
        # Outside except on purpose: yt-dlp's leaked .part handle sits in a traceback cycle (WinError 32 on reuse).
        gc.collect()
        if staging is not None:
            _discard_staging(final_path)
        raise DownloadFailedError(EXCEPTIONS_PL["download"]["failed"])

    if staging is not None:
        if not staging.is_file():
            _discard_staging(final_path)
            raise DownloadFailedError(EXCEPTIONS_PL["download"]["failed"])
        _promote_staging(staging, final_path)

    if final_path.exists():
        progress.success(CONTENT["download"]["saved"].format(path=final_path))
    return True
