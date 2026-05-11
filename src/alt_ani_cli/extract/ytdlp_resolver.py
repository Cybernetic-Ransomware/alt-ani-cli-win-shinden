from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.extract.common import Stream


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


def resolve(
    embed_url: str,
    referer: str,
    *,
    cookies_file: str | None = None,
    cookies_browser: str | None = None,
) -> Stream:
    from yt_dlp import YoutubeDL  # deferred — yt-dlp startup is slow

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": _SilentLogger(),
        "http_headers": {
            "Referer": referer,
            "User-Agent": USER_AGENT,
        },
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(embed_url, download=False)
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    if not info:
        raise ValueError(EXCEPTIONS["ytdlp"]["no_info"].format(embed_url=repr(embed_url)))

    qualities: dict[str, str] = {}
    best_url = ""
    best_height = 0

    for fmt in info.get("formats") or []:
        fmt_url = fmt.get("url") or fmt.get("manifest_url", "")
        height = fmt.get("height") or 0
        if fmt_url and height:
            qualities[f"{height}p"] = fmt_url
            if height > best_height:
                best_height = height
                best_url = fmt_url

    if not best_url:
        best_url = info.get("url") or info.get("manifest_url", "")

    if not best_url:
        raise ValueError(EXCEPTIONS["ytdlp"]["no_url"].format(embed_url=repr(embed_url)))

    ext = info.get("ext") or ("m3u8" if "m3u8" in best_url else "mp4")
    headers = dict(info.get("http_headers") or {})
    headers.setdefault("Referer", referer)
    headers.setdefault("User-Agent", USER_AGENT)

    return Stream(url=best_url, headers=headers, qualities=qualities, ext=ext)
