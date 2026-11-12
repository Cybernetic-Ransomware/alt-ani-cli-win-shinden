import gc
import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import USER_AGENT, YTDLP_SOCKET_TIMEOUT_SEC
from alt_ani_core.errors import DownloadFailedError, DownloadTargetError, EngineInterrupt
from alt_ani_core.events import (
    NULL_SINK,
    DownloadLog,
    DownloadSaved,
    DownloadStarted,
    EventSink,
    LegacyPartMoved,
)
from alt_ani_core.messages import text
from alt_ani_core.models import EpisodeRow, SeriesRef, StreamProtocol, StreamVariant
from alt_ani_core.redaction import redact_text

_SUPPRESS_PREFIXES = (
    "[generic] Extracting URL:",
    "[redirect] Following redirect to",
    "[info] ",
)

_LEGACY_PART_SUFFIX = ".legacy-ffmpeg"
_STAGING_INFIX = ".redownload"


class _EventLogger:
    """yt-dlp logger that forwards redacted lines as events and drops noisy extraction chatter."""

    def __init__(self, events: EventSink) -> None:
        self._events = events

    def _emit(self, msg: str) -> None:
        if any(msg.startswith(p) for p in _SUPPRESS_PREFIXES):
            return
        self._events.emit(DownloadLog(redacted_line=redact_text(msg)))

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


def _output_template(stream: StreamVariant, base: Path) -> str:
    # Generic extraction names extensionless direct URLs "unknown_video"; segmented streams keep yt-dlp's ext (remux).
    ext = stream.container if stream.protocol is StreamProtocol.PROGRESSIVE and stream.container else "%(ext)s"
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


def _target_error(code: str, path: Path) -> DownloadTargetError:
    return DownloadTargetError(text(code, path=path)).coded(code, path=str(path))


def _failed() -> DownloadFailedError:
    return DownloadFailedError(text("download.failed")).coded("download.failed")


def _redirect_to_staging(ydl, info: dict, final_path: Path) -> Path:
    """Point yt-dlp at a sibling staging file so the existing final stays intact until the new copy is complete."""
    # A staging partial may come from another source or a crashed run; resuming or promoting it could splice stale data.
    if leftovers := _discard_staging(final_path):
        raise _target_error("download.staging_locked", leftovers[0])
    staging = _staging_path(final_path)
    ydl.params["outtmpl"]["default"] = str(staging).replace("%", "%%")
    if Path(ydl.prepare_filename(info)) != staging:
        raise _target_error("download.staging_mismatch", final_path)
    return staging


def _promote_staging(staging: Path, final_path: Path) -> None:
    try:
        os.replace(staging, final_path)
    except OSError:
        _discard_staging(final_path)
        raise _target_error("download.replace_failed", final_path) from None


def run(
    stream: StreamVariant,
    ep: EpisodeRow,
    series: SeriesRef,
    dest_dir: Path,
    *,
    confirm_overwrite: Callable[[Path], bool] | None = None,
    events: EventSink = NULL_SINK,
    cancel: CancelToken = NEVER,
) -> bool:
    """Returns False when the final was kept; the post-yt-dlp cancel check is the commit point for replacing it."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(c for c in series.title if c.isalnum() or c in " _-").strip()
    ep_label = f"{ep.number:g}"

    def _stop_when_cancelled(_status: dict) -> None:
        cancel.raise_if_cancelled()

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
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SEC,
        "quiet": True,
        "noprogress": True,
        "logger": _EventLogger(events),
        "progress_hooks": [_stop_when_cancelled],
    }

    events.emit(DownloadStarted(title=safe_title, episode_label=ep_label, directory=str(dest_dir)))
    failed = False
    interrupted = False
    staging: Path | None = None
    final_path: Path | None = None
    try:
        with YoutubeDL(opts) as ydl:
            cancel.raise_if_cancelled()
            info = ydl.extract_info(stream.url, download=False)
            final_path = Path(ydl.prepare_filename(info))
            if confirm_overwrite is not None and final_path.exists():
                if not confirm_overwrite(final_path):
                    return False
                staging = _redirect_to_staging(ydl, info, final_path)
            target = staging or final_path
            if str(info.get("protocol", "")).startswith("m3u8") and (backup := _quarantine_legacy_hls_part(target)):
                events.emit(LegacyPartMoved(path=str(backup)))
            cancel.raise_if_cancelled()
            ydl.process_ie_result(info, download=True)
            cancel.raise_if_cancelled()
    except DownloadError:
        failed = True
    except EngineInterrupt, KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        if interrupted and staging is not None and final_path is not None:
            # same handle caveat as below; an interrupted redownload must not leave a stale staging copy to be promoted
            gc.collect()
            _discard_staging(final_path)

    if failed or final_path is None:
        # Outside except on purpose: yt-dlp's leaked .part handle sits in a traceback cycle (WinError 32 on reuse).
        gc.collect()
        if staging is not None and final_path is not None:
            _discard_staging(final_path)
        cancel.raise_if_cancelled()
        raise _failed()

    if staging is not None:
        if not staging.is_file():
            _discard_staging(final_path)
            raise _failed()
        _promote_staging(staging, final_path)

    if final_path.exists():
        events.emit(DownloadSaved(path=str(final_path)))
    return True
