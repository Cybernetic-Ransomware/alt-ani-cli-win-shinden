from alt_ani_core.config import USER_AGENT, YTDLP_SOCKET_TIMEOUT_SEC, ExtractOptions
from alt_ani_core.extract.common import (
    CATEGORY_NO_STREAM_URL,
    CATEGORY_PARSER_DRIFT,
    ExtractError,
    Stream,
    StreamProtocol,
    container_of,
    sniff_protocol,
)
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.models import StreamVariant


def _cause_chain(exc: BaseException) -> list[BaseException]:
    # yt-dlp nests the real error in DownloadError.exc_info and ExtractorError/RequestError.cause
    chain: list[BaseException] = []
    pending: list[object] = [exc]
    while pending and len(chain) < 16:
        cur = pending.pop(0)
        if not isinstance(cur, BaseException) or any(cur is seen for seen in chain):
            continue
        chain.append(cur)
        exc_info = getattr(cur, "exc_info", None)
        nested = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
        pending += [nested, getattr(cur, "cause", None), cur.__cause__]
    return chain


def _classify_ytdlp_error(exc: BaseException) -> tuple[str, int | None]:
    from yt_dlp.networking.exceptions import HTTPError, TransportError
    from yt_dlp.utils import RegexNotFoundError, UnsupportedError

    chain = _cause_chain(exc)
    for e in chain:
        if isinstance(e, HTTPError):
            status = getattr(e, "status", None)
            return "http_error", status if isinstance(status, int) else None
    if any(isinstance(e, TimeoutError) for e in chain):
        return "timeout", None
    if any(isinstance(e, TransportError) for e in chain):
        return "network_error", None
    if any(isinstance(e, UnsupportedError) for e in chain):
        return "unsupported_host", None
    if any(isinstance(e, RegexNotFoundError) for e in chain):
        return CATEGORY_PARSER_DRIFT, None
    return "unknown", None


class _SilentLogger:
    """Suppress all yt-dlp console output — errors are re-raised as exceptions."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _protocol(fmt: dict, url: str) -> StreamProtocol:
    """yt-dlp names the protocol of every format; the URL is only consulted when it does not."""
    name = str(fmt.get("protocol") or "")
    if name.startswith("m3u8"):
        return StreamProtocol.HLS
    if "dash" in name:
        return StreamProtocol.DASH
    if name in ("http", "https"):
        return StreamProtocol.PROGRESSIVE
    return sniff_protocol(url)


def _variant(fmt: dict, url: str, headers: dict[str, str], label: str = "") -> StreamVariant:
    protocol = _protocol(fmt, url)
    ext = fmt.get("ext")
    container = ext if protocol is StreamProtocol.PROGRESSIVE and isinstance(ext, str) and ext else None
    return StreamVariant(
        url=url, protocol=protocol, headers=headers, label=label, container=container or container_of(url, protocol)
    )


def resolve(embed_url: str, referer: str, options: ExtractOptions | None = None) -> Stream:
    from yt_dlp import YoutubeDL  # deferred — yt-dlp startup is slow

    options = options or ExtractOptions()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SEC,
        "logger": _SilentLogger(),
        "http_headers": {
            "Referer": referer,
            "User-Agent": USER_AGENT,
        },
    }
    if options.cookies_file:
        opts["cookiefile"] = options.cookies_file
    if options.cookies_browser:
        opts["cookiesfrombrowser"] = (options.cookies_browser,)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(embed_url, download=False)
    except Exception as exc:
        category, http_status = _classify_ytdlp_error(exc)
        raise ExtractError(str(exc), category, http_status) from exc

    if not info:
        raise ExtractError(EXCEPTIONS["ytdlp"]["no_info"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)

    headers = dict(info.get("http_headers") or {})
    headers.setdefault("Referer", referer)
    headers.setdefault("User-Agent", USER_AGENT)

    by_label: dict[str, StreamVariant] = {}
    best_label = ""
    best_height = 0
    for fmt in info.get("formats") or []:
        fmt_url = fmt.get("url") or fmt.get("manifest_url", "")
        height = fmt.get("height") or 0
        if fmt_url and height:
            label = f"{height}p"
            by_label[label] = _variant(fmt, fmt_url, headers, label)
            if height > best_height:
                best_height = height
                best_label = label

    if by_label:
        variants = tuple(by_label.values())
        return Stream(variants=variants, default_index=list(by_label).index(best_label))

    url = info.get("url") or info.get("manifest_url", "")
    if not url:
        raise ExtractError(EXCEPTIONS["ytdlp"]["no_url"].format(embed_url=repr(embed_url)), CATEGORY_NO_STREAM_URL)
    return Stream(variants=(_variant(info, url, headers),))
