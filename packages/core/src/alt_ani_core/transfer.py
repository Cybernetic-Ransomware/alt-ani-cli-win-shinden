"""Executor transfer: yt-dlp writes, but each response adding bytes is checked against recorded evidence first."""

import gc
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import YTDLP_SOCKET_TIMEOUT_SEC
from alt_ani_core.download import _EventLogger, base_options
from alt_ani_core.errors import FAILED, EngineInterrupt, ShindenError, classify_os_error
from alt_ani_core.events import NULL_SINK, EventSink
from alt_ani_core.models import StreamVariant
from alt_ani_core.rules.resume import KIND_HLS, KIND_HTTP, checkpoint_of, identity_fields
from alt_ani_core.state import ResourceValidator

MANIFEST_FILE = "validators.json"
MEDIA_STEM = "media"
MAX_SWEEP_SEGMENTS = 5000


class ResumeRefused(BaseException):  # noqa: N818 - control flow, like EngineInterrupt
    """The partial cannot be continued with evidence; nothing was written. A BaseException so yt-dlp cannot retry it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TransferFailed(ShindenError):
    """yt-dlp gave up; ``category`` and ``http_status`` only, the message never carries the URL."""

    def __init__(self, category: str, http_status: int | None = None) -> None:
        super().__init__(f"transfer failed: {category}")
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class TransferResult:
    media: Path
    size: int
    protocol: str
    extension: str
    fragments: int | None = None


@dataclass
class TransferCallbacks:
    """``validator`` runs before the first byte is written; ``checkpoint`` only after the data it names is fsynced."""

    validator: Callable[[ResourceValidator], None] = lambda _v: None
    progress: Callable[[int, int | None], None] = lambda _d, _t: None
    checkpoint: Callable[[int, int], None] = lambda _length, _fragments: None


@dataclass(frozen=True)
class CheckpointPolicy:
    interval_sec: float = 5.0
    every_bytes: int = 32 * 1024 * 1024
    clock: Callable[[], float] = time.monotonic


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strong_etag(value: str | None) -> str | None:
    """Weak validators (``W/``) may stay the same while bytes change, so they are never evidence."""
    if not value or value.startswith(("W/", "w/")) or not value.startswith('"'):
        return None
    return value


def fsync_path(path: Path) -> None:
    # opened for writing: Windows flushes a file only through a handle with write access
    fd = os.open(path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class MediaPlaylist:
    """What identifies a media playlist without its URIs (which carry tokens), plus the absolute segment URLs."""

    segments: tuple[str, ...]
    structure: str
    resumable: bool


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    durations: list[str] = []
    segments: list[str] = []
    sequence = "0"
    has_map = False
    resumable = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            durations.append(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            # decrypted bytes also depend on a key the manifest does not cover
            resumable = False
        elif line.startswith("#EXT-X-BYTERANGE"):
            resumable = False
        elif line.startswith("#EXT-X-MAP"):
            has_map = True
            resumable = False
        elif line.startswith("#EXT-X-STREAM-INF"):
            resumable = False
        elif not line.startswith("#"):
            segments.append(urljoin(base_url, line))
    structure = _digest(f"seq={sequence};n={len(segments)};d={','.join(durations)};map={has_map}")
    return MediaPlaylist(tuple(segments), structure, resumable and bool(segments))


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / MANIFEST_FILE


def write_manifest(work_dir: Path, etags: list[str]) -> str:
    """Hashes of the segment ETags only; returns the digest of the file the database keeps."""
    body = json.dumps({"etags": etags}, sort_keys=True)
    path = _manifest_path(work_dir)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(body, encoding="utf-8")
    fsync_path(temp)
    os.replace(temp, path)
    return _digest(body)


def read_manifest(work_dir: Path, expected_digest: str) -> list[str] | None:
    try:
        body = _manifest_path(work_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    if _digest(body) != expected_digest:
        return None
    etags = json.loads(body).get("etags")
    return etags if isinstance(etags, list) and all(isinstance(e, str) for e in etags) else None


def _ytdl_state(index: int) -> str:
    return json.dumps({"downloader": {"current_fragment": {"index": index}, "extra_state": {}}})


def read_ytdl_index(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        index = data["downloader"]["current_fragment"]["index"]
    except OSError, ValueError, KeyError, TypeError:
        return None
    return index if isinstance(index, int) and index >= 0 else None


def _header(response, name: str) -> str | None:
    value = response.headers.get(name)
    return value if isinstance(value, str) else None


def _content_range(value: str | None) -> tuple[int, int | None] | None:
    if not value or not value.startswith("bytes "):
        return None
    span, _, total = value[6:].partition("/")
    start, _, _ = span.partition("-")
    try:
        return int(start), None if total == "*" else int(total)
    except ValueError:
        return None


def _range_start(request) -> int | None:
    value = request.headers.get("Range")
    if not value or not value.startswith("bytes="):
        return None
    try:
        return int(value[6:].split("-", 1)[0])
    except ValueError:
        return None


@dataclass
class _Guard:
    """Decides, per request, whether the response may reach the downloader; armed only for the media transfer."""

    kind: str
    media_url: str = ""
    etag: str | None = None
    total: int | None = None
    on_capture: Callable[[str | None, int | None], None] | None = None
    resume_from: int = 0
    segments: dict[str, int] = field(default_factory=dict)
    manifest: list[str] | None = None
    next_index: int = 0
    last_url: str | None = None
    armed: bool = False

    def check(self, request, send):
        if not self.armed or isinstance(request, str):
            return send(request)
        if self.kind == KIND_HTTP:
            return self._check_http(request, send)
        if self.kind == KIND_HLS:
            return self._check_segment(request, send)
        return send(request)

    def _check_http(self, request, send):
        if request.url != self.media_url:
            return send(request)
        start = _range_start(request) or 0
        if start > 0:
            if self.etag is None:
                raise ResumeRefused("no_validator")
            request.headers["If-Range"] = self.etag
        elif self.resume_from > 0:
            # yt-dlp asking from zero would truncate the partial
            raise ResumeRefused("restart_requested")
        response = send(request)
        etag = strong_etag(_header(response, "ETag"))
        if start == 0:
            total = _total_of(response)
            if self.on_capture is not None:
                capture, self.on_capture = self.on_capture, None
                self.etag, self.total = etag, total
                capture(etag, total)
            elif etag is None or etag != self.etag or total != self.total:
                response.close()
                raise ResumeRefused("resource_changed")
            return response
        content_range = _content_range(_header(response, "Content-Range"))
        if response.status != 206 or content_range is None or content_range[0] != start:
            response.close()
            raise ResumeRefused("range_ignored")
        if etag is None or etag != self.etag or content_range[1] != self.total:
            response.close()
            raise ResumeRefused("resource_changed")
        return response

    def _check_segment(self, request, send):
        url = request.url
        if url in self.segments:
            index = self.segments[url]
        elif url == self.last_url:
            index = self.next_index - 1
        elif url.split("?", 1)[0].endswith((".m3u8", ".m3u")):
            return send(request)
        else:
            index = self.next_index
        if url != self.last_url:
            self.last_url, self.next_index = url, index + 1
        response = send(request)
        if self.manifest is not None:
            etag = strong_etag(_header(response, "ETag"))
            if index >= len(self.manifest) or etag is None or _digest(etag) != self.manifest[index]:
                response.close()
                raise ResumeRefused("segment_changed")
        return response


def _total_of(response) -> int | None:
    content_range = _content_range(_header(response, "Content-Range"))
    if content_range is not None:
        return content_range[1]
    length = _header(response, "Content-Length")
    return int(length) if length and length.isdigit() else None


def _bound_ydl(opts: dict, guard: _Guard):
    from yt_dlp import YoutubeDL

    class BoundYoutubeDL(YoutubeDL):
        def urlopen(self, req):
            return guard.check(req, super().urlopen)

    return BoundYoutubeDL(opts)


class _StopLogger(_EventLogger):
    """yt-dlp retries a stalled read without calling progress hooks; its retry warnings are a stop checkpoint too."""

    def __init__(self, events: EventSink, cancel: CancelToken) -> None:
        super().__init__(events)
        self._cancel = cancel

    def warning(self, msg: str) -> None:
        self._cancel.raise_if_cancelled()
        super().warning(msg)

    def error(self, msg: str) -> None:
        self._cancel.raise_if_cancelled()
        super().error(msg)


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    pending: list[object] = [exc]
    while pending and len(chain) < 16:
        cur = pending.pop(0)
        if not isinstance(cur, BaseException) or any(cur is seen for seen in chain):
            continue
        chain.append(cur)
        exc_info = getattr(cur, "exc_info", None)
        nested = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
        pending += [nested, getattr(cur, "cause", None), cur.__cause__, cur.__context__]
    return chain


def classify(exc: BaseException) -> TransferFailed:
    from yt_dlp.networking.exceptions import HTTPError, TransportError

    for cur in _cause_chain(exc):
        if isinstance(cur, HTTPError):
            status = getattr(cur, "status", None)
            return TransferFailed("http_error", status if isinstance(status, int) else None)
    for cur in _cause_chain(exc):
        if isinstance(cur, OSError) and not isinstance(cur, TimeoutError | ConnectionError):
            code = classify_os_error(cur)
            if code != FAILED:
                return TransferFailed(code)
    if any(isinstance(cur, TimeoutError) for cur in _cause_chain(exc)):
        return TransferFailed("timeout")
    if any(isinstance(cur, TransportError | ConnectionError) for cur in _cause_chain(exc)):
        return TransferFailed("network_error")
    return TransferFailed("unknown")


def _fetch_text(ydl, url: str, headers: dict, cancel: CancelToken) -> str:
    from yt_dlp.networking import Request

    cancel.raise_if_cancelled()
    with ydl.urlopen(Request(url, headers=headers)) as response:
        return response.read().decode("utf-8", errors="replace")


def _segment_etag(ydl, url: str, headers: dict, cancel: CancelToken) -> str | None:
    from yt_dlp.networking import Request

    cancel.raise_if_cancelled()
    try:
        with ydl.urlopen(Request(url, headers=headers, method="HEAD")) as response:
            return strong_etag(_header(response, "ETag"))
    except Exception:
        return None


def _segment_manifest(ydl, playlist: MediaPlaylist, headers: dict, cancel: CancelToken) -> list[str] | None:
    if not playlist.resumable or len(playlist.segments) > MAX_SWEEP_SEGMENTS:
        return None
    hashes: list[str] = []
    for url in playlist.segments:
        etag = _segment_etag(ydl, url, headers, cancel)
        if etag is None:
            return None
        hashes.append(_digest(etag))
    return hashes


def _truncate(path: Path, length: int) -> None:
    with path.open("r+b") as fh:
        fh.truncate(length)
        fh.flush()
        os.fsync(fh.fileno())


def _protocol_kind(info: dict) -> str:
    protocol = str(info.get("protocol") or "")
    if protocol in ("http", "https"):
        return KIND_HTTP
    if protocol == "m3u8_native":
        return KIND_HLS
    return protocol or "unknown"


def transfer(
    variant: StreamVariant,
    work_dir: Path,
    *,
    resume: ResourceValidator | None = None,
    callbacks: TransferCallbacks | None = None,
    policy: CheckpointPolicy | None = None,
    events: EventSink = NULL_SINK,
    cancel: CancelToken = NEVER,
    socket_timeout: float = YTDLP_SOCKET_TIMEOUT_SEC,
) -> TransferResult:
    """Fresh when ``resume`` is None, else continues or raises ``ResumeRefused``; returning means the I/O has ended."""
    from yt_dlp.utils import DownloadError

    callbacks = callbacks or TransferCallbacks()
    policy = policy or CheckpointPolicy()
    guard = _Guard(kind="unknown")
    paths: dict[str, Path] = {}
    progress = {"last_at": policy.clock(), "last_len": 0, "fragments": 0}

    def take_checkpoint() -> None:
        part = paths.get("part")
        if part is None or not part.exists():
            return
        if guard.kind == KIND_HLS:
            # between hooks yt-dlp has both appended the fragment and written .ytdl, so the pair is consistent
            index = read_ytdl_index(paths["ytdl"])
            if index is None:
                return
        else:
            index = 0
        length = part.stat().st_size
        fsync_path(part)
        callbacks.checkpoint(length, index)
        progress["last_at"], progress["last_len"] = policy.clock(), length

    def hook(status: dict) -> None:
        if status.get("status") == "downloading" and isinstance(status.get("fragment_index"), int):
            progress["fragments"] = status["fragment_index"]
        if cancel.cancelled:
            take_checkpoint()
            cancel.raise_if_cancelled()
        done = status.get("downloaded_bytes")
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if isinstance(done, int):
            callbacks.progress(done, int(total) if isinstance(total, int | float) else None)
            due_time = policy.clock() - progress["last_at"] >= policy.interval_sec
            due_bytes = done - progress["last_len"] >= policy.every_bytes
            if status.get("status") == "downloading" and (due_time or due_bytes):
                take_checkpoint()

    opts = base_options(variant, _StopLogger(events, cancel), [hook], socket_timeout=socket_timeout)
    opts |= {
        "outtmpl": os.path.join(str(work_dir).replace("%", "%%"), f"{MEDIA_STEM}.%(ext)s"),
        "continuedl": True,
        "concurrent_fragment_downloads": 1,
        "postprocessor_hooks": [lambda _status: cancel.raise_if_cancelled()],
    }
    if variant.container:
        opts["outtmpl"] = os.path.join(str(work_dir).replace("%", "%%"), f"{MEDIA_STEM}.{variant.container}")
    headers = dict(opts["http_headers"])
    try:
        with _bound_ydl(opts, guard) as ydl:
            cancel.raise_if_cancelled()
            info = ydl.extract_info(variant.url, download=False)
            cancel.raise_if_cancelled()
            media = Path(ydl.prepare_filename(info))
            if media.parent != work_dir:
                raise ResumeRefused("unexpected_path")
            paths |= {"media": media, "part": Path(f"{media}.part"), "ytdl": Path(f"{media}.ytdl")}
            guard.kind = _protocol_kind(info)
            _prepare(guard, info, ydl, headers, work_dir, paths, resume, callbacks, cancel)
            guard.armed = True
            ydl.process_ie_result(info, download=True)
            cancel.raise_if_cancelled()
    except DownloadError as exc:
        failure = classify(exc)
        gc.collect()
        cancel.raise_if_cancelled()
        raise failure from None
    except EngineInterrupt, ResumeRefused:
        gc.collect()
        raise
    media = paths["media"]
    if not media.is_file():
        raise TransferFailed("missing_output")
    return TransferResult(
        media=media,
        size=media.stat().st_size,
        protocol=guard.kind,
        extension=media.suffix.lstrip("."),
        fragments=len(guard.segments) if guard.kind == KIND_HLS else None,
    )


def _prepare(
    guard: _Guard,
    info: dict,
    ydl,
    headers: dict,
    work_dir: Path,
    paths: dict[str, Path],
    resume: ResourceValidator | None,
    callbacks: TransferCallbacks,
    cancel: CancelToken,
) -> None:
    media_url = str(info.get("url") or "")
    if guard.kind == KIND_HTTP:
        guard.media_url = media_url
        if resume is None:
            for leftover in (paths["part"], paths["media"]):
                leftover.unlink(missing_ok=True)

            def capture(etag: str | None, total: int | None) -> None:
                fields: dict[str, str | int] = {"length": 0}
                if total is not None:
                    fields["total"] = total
                if etag is not None:
                    fields["etag"] = _digest(etag)
                callbacks.validator(ResourceValidator(KIND_HTTP, tuple(fields.items())))

            guard.on_capture = capture
            return
        _resume_http(guard, ydl, headers, paths, resume, cancel)
        return
    if guard.kind == KIND_HLS:
        playlist = parse_media_playlist(_fetch_text(ydl, media_url, headers, cancel), media_url)
        guard.segments = {url: i for i, url in enumerate(playlist.segments)}
        if resume is None:
            for leftover in work_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink()
            manifest = _segment_manifest(ydl, playlist, headers, cancel)
            manifest_digest = write_manifest(work_dir, manifest) if manifest is not None else ""
            guard.manifest = manifest
            fields = {
                "playlist": playlist.structure,
                "segments": len(playlist.segments),
                "manifest": manifest_digest,
                "length": 0,
                "fragments": 0,
            }
            callbacks.validator(ResourceValidator(KIND_HLS, tuple(fields.items())))
            return
        _resume_hls(guard, playlist, ydl, headers, work_dir, paths, resume, cancel)
        return
    if resume is not None:
        raise ResumeRefused("unsupported_protocol")
    callbacks.validator(ResourceValidator(guard.kind, (("length", 0),)))


def _resume_http(guard: _Guard, ydl, headers: dict, paths: dict[str, Path], resume: ResourceValidator, cancel) -> None:
    """Only the ETag's hash is stored: a HEAD learns the value, which then rides on ``If-Range`` of the real request."""
    fields = identity_fields(resume)
    length, _ = checkpoint_of(resume)
    etag_hash, total = fields.get("etag"), fields.get("total")
    if resume.kind != KIND_HTTP or not isinstance(etag_hash, str) or not isinstance(total, int) or length <= 0:
        raise ResumeRefused("no_validator")
    part = paths["part"]
    if not part.is_file() or part.stat().st_size < length:
        raise ResumeRefused("partial_shorter_than_checkpoint")
    etag = _segment_etag(ydl, guard.media_url, headers, cancel)
    if etag is None or _digest(etag) != etag_hash:
        raise ResumeRefused("resource_changed")
    _truncate(part, length)
    paths["media"].unlink(missing_ok=True)
    guard.resume_from, guard.total, guard.etag = length, total, etag


def _resume_hls(
    guard: _Guard,
    playlist: MediaPlaylist,
    ydl,
    headers: dict,
    work_dir: Path,
    paths: dict[str, Path],
    resume: ResourceValidator,
    cancel: CancelToken,
) -> None:
    fields = identity_fields(resume)
    length, fragments = checkpoint_of(resume)
    manifest_digest = fields.get("manifest")
    if resume.kind != KIND_HLS or not isinstance(manifest_digest, str) or not manifest_digest or length <= 0:
        raise ResumeRefused("no_validator")
    if fields.get("playlist") != playlist.structure or fields.get("segments") != len(playlist.segments):
        raise ResumeRefused("playlist_changed")
    manifest = read_manifest(work_dir, manifest_digest)
    if manifest is None or len(manifest) != len(playlist.segments) or not 0 < fragments <= len(manifest):
        raise ResumeRefused("manifest_unavailable")
    for index in range(fragments):
        etag = _segment_etag(ydl, playlist.segments[index], headers, cancel)
        if etag is None or _digest(etag) != manifest[index]:
            raise ResumeRefused("segment_changed")
    part = paths["part"]
    if not part.is_file() or part.stat().st_size < length:
        raise ResumeRefused("partial_shorter_than_checkpoint")
    _truncate(part, length)
    for leftover in work_dir.glob(f"{paths['media'].name}.part-Frag*"):
        leftover.unlink()
    ytdl = paths["ytdl"]
    temp = ytdl.with_name(ytdl.name + ".tmp")
    temp.write_text(_ytdl_state(fragments), encoding="utf-8")
    os.replace(temp, ytdl)
    paths["media"].unlink(missing_ok=True)
    guard.manifest = manifest
    guard.next_index = fragments
