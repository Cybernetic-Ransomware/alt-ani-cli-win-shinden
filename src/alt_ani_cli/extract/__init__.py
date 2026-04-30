import re
from urllib.parse import urlparse

from alt_ani_cli.errors import NoStreamError
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.extract import dood, jwplayer, mp4upload, streamtape, ytdlp_resolver

# ebd.cda.pl/800x450/{id} → yt-dlp only understands player.cda.pl/play/{id}
_EBD_CDA_RE = re.compile(r"/\d+x\d+/([0-9a-z]+)$", re.IGNORECASE)


def _normalize_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host == "ebd.cda.pl":
        m = _EBD_CDA_RE.search(url)
        if m:
            return f"https://www.cda.pl/video/{m.group(1)}"
    return url

# Hosts that serve a pure JS SPA — no video URL in initial HTML, requires a browser.
# Fail fast for these instead of wasting 5 s antibot + HTTP roundtrip.
_JS_ONLY_HOSTS = {
    "bysesukior.com",
    "www.bysesukior.com",
    "voe.sx",
    "www.voe.sx",
}

_YTDLP_HOSTS = {
    "cda.pl",
    "www.cda.pl",
    "player.cda.pl",
    "ebd.cda.pl",
    "sibnet.ru",
    "video.sibnet.ru",
    "vk.com",
    "vkvideo.ru",
}

_CUSTOM: dict = {
    # mp4upload
    "mp4upload.com": mp4upload.resolve,
    "www.mp4upload.com": mp4upload.resolve,
    # streamtape
    "streamtape.com": streamtape.resolve,
    "www.streamtape.com": streamtape.resolve,
    "streamtape.to": streamtape.resolve,
    "streamtape.xyz": streamtape.resolve,
    # dood family
    "doodstream.com": dood.resolve,
    "www.doodstream.com": dood.resolve,
    "dood.la": dood.resolve,
    "dood.re": dood.resolve,
    "dooood.com": dood.resolve,
    "ds2play.com": dood.resolve,
    # streamwish / playerwish (JWPlayer)
    "streamwish.com": jwplayer.resolve,
    "www.streamwish.com": jwplayer.resolve,
    "playerwish.com": jwplayer.resolve,
    "wishembed.net": jwplayer.resolve,
    "streamwish.to": jwplayer.resolve,
    # filemoon (JWPlayer)
    "filemoon.sx": jwplayer.resolve,
    "filemoon.to": jwplayer.resolve,
    "kerapoxy.cc": jwplayer.resolve,
    # other JWPlayer-based hosts common on shinden
    "streamvid.net": jwplayer.resolve,
    "vidmoly.to": jwplayer.resolve,
    "vidhide.com": jwplayer.resolve,
    "vidhidepro.com": jwplayer.resolve,
    "embedwish.com": jwplayer.resolve,
    "alions.pro": jwplayer.resolve,
}


def resolve(
    embed_url: str,
    referer: str,
    *,
    cookies_file: str | None = None,
    cookies_browser: str | None = None,
) -> Stream:
    """Dispatch to the right extractor based on the embed URL hostname."""
    embed_url = _normalize_url(embed_url)
    host = urlparse(embed_url).netloc.lower()

    _ytdlp_kw = {"cookies_file": cookies_file, "cookies_browser": cookies_browser}

    if host in _JS_ONLY_HOSTS:
        raise NoStreamError(
            f"{host} requires JavaScript execution (pure SPA — no static video URL)"
        )

    if host in _YTDLP_HOSTS:
        try:
            return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
        except Exception as exc:
            raise NoStreamError(
                f"yt-dlp could not extract stream from {embed_url!r}: {exc}"
            ) from exc

    custom_fn = _CUSTOM.get(host)
    if custom_fn:
        try:
            return custom_fn(embed_url, referer)
        except Exception as exc:
            try:
                return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
            except Exception:
                raise NoStreamError(
                    f"All extractors failed for {embed_url!r}"
                ) from exc

    # Unknown host — try JWPlayer first (covers most embed-site patterns),
    # then fall back to yt-dlp (1500+ supported sites).
    try:
        return jwplayer.resolve(embed_url, referer)
    except Exception:
        pass

    try:
        return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
    except Exception as exc:
        raise NoStreamError(
            f"All extractors failed for {embed_url!r}: {exc}"
        ) from exc
