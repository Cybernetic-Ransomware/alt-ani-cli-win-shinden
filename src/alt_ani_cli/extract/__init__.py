import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from alt_ani_cli.content import EXCEPTIONS
from alt_ani_cli.errors import JavaScriptRequiredError, NoStreamError, UnsupportedHostError
from alt_ani_cli.extract import dood, jwplayer, lycoris, mp4upload, streamtape, vidara, ytdlp_resolver
from alt_ani_cli.extract.common import Stream

# ebd.cda.pl/800x450/{id} → yt-dlp does not understand the embed URL; rewrite to www.cda.pl/video/{id}
_EBD_CDA_RE = re.compile(r"/\d+x\d+/([0-9a-z]+)$", re.IGNORECASE)


def _normalize_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host == "ebd.cda.pl":
        m = _EBD_CDA_RE.search(url)
        if m:
            return f"https://www.cda.pl/video/{m.group(1)}"
    return url


@dataclass(frozen=True)
class HostRule:
    """Routing rule for a known embed host.

    - ``custom`` — native extractor in ``resolver``, falls back to yt-dlp on failure.
    - ``jwplayer`` — JWPlayer-based host, shorthand for ``custom`` with ``jwplayer.resolve``;
      falls back to yt-dlp on failure.
    - ``ytdlp`` — go straight to yt-dlp, no JWPlayer attempt and no fallback.
    - ``unsupported`` — fail fast; ``reason`` is an ``EXCEPTIONS["extract"]`` message key.
    """

    mode: Literal["custom", "jwplayer", "ytdlp", "unsupported"]
    resolver: Callable[[str, str], Stream] | None = None
    reason: str | None = None


HOST_RULES: dict[str, HostRule] = {
    # pure JS SPA — no video URL in initial HTML, requires a browser;
    # fail fast instead of wasting 5 s antibot + HTTP roundtrip
    "bysesukior.com": HostRule("unsupported", reason="js_only_host"),
    "www.bysesukior.com": HostRule("unsupported", reason="js_only_host"),
    "voe.sx": HostRule("unsupported", reason="js_only_host"),
    "www.voe.sx": HostRule("unsupported", reason="js_only_host"),
    # yt-dlp handles these natively — skip the JWPlayer attempt
    "cda.pl": HostRule("ytdlp"),
    "www.cda.pl": HostRule("ytdlp"),
    "player.cda.pl": HostRule("ytdlp"),
    "ebd.cda.pl": HostRule("ytdlp"),
    "sibnet.ru": HostRule("ytdlp"),
    "video.sibnet.ru": HostRule("ytdlp"),
    "vk.com": HostRule("ytdlp"),
    "vkvideo.ru": HostRule("ytdlp"),
    "pixeldrain.com": HostRule("ytdlp"),
    "www.pixeldrain.com": HostRule("ytdlp"),
    # mp4upload
    "mp4upload.com": HostRule("custom", mp4upload.resolve),
    "www.mp4upload.com": HostRule("custom", mp4upload.resolve),
    # streamtape
    "streamtape.com": HostRule("custom", streamtape.resolve),
    "www.streamtape.com": HostRule("custom", streamtape.resolve),
    "streamtape.to": HostRule("custom", streamtape.resolve),
    "streamtape.xyz": HostRule("custom", streamtape.resolve),
    # dood family
    "doodstream.com": HostRule("custom", dood.resolve),
    "www.doodstream.com": HostRule("custom", dood.resolve),
    "dood.la": HostRule("custom", dood.resolve),
    "dood.re": HostRule("custom", dood.resolve),
    "dooood.com": HostRule("custom", dood.resolve),
    "ds2play.com": HostRule("custom", dood.resolve),
    # vidara
    "vidara.to": HostRule("custom", vidara.resolve),
    "www.vidara.to": HostRule("custom", vidara.resolve),
    # lycoris
    "lycoris.cafe": HostRule("custom", lycoris.resolve),
    "www.lycoris.cafe": HostRule("custom", lycoris.resolve),
    # streamwish / playerwish
    "streamwish.com": HostRule("jwplayer"),
    "www.streamwish.com": HostRule("jwplayer"),
    "playerwish.com": HostRule("jwplayer"),
    "wishembed.net": HostRule("jwplayer"),
    "streamwish.to": HostRule("jwplayer"),
    # filemoon
    "filemoon.sx": HostRule("jwplayer"),
    "filemoon.to": HostRule("jwplayer"),
    "kerapoxy.cc": HostRule("jwplayer"),
    # other JWPlayer-based hosts common on shinden
    "streamvid.net": HostRule("jwplayer"),
    "vidmoly.to": HostRule("jwplayer"),
    "vidhide.com": HostRule("jwplayer"),
    "vidhidepro.com": HostRule("jwplayer"),
    "embedwish.com": HostRule("jwplayer"),
    "alions.pro": HostRule("jwplayer"),
}


def resolve(
    embed_url: str,
    referer: str,
    *,
    cookies_file: str | None = None,
    cookies_browser: str | None = None,
    on_fallback: Callable[[str, str, Exception], None] | None = None,
) -> Stream:
    """Dispatch to the right extractor based on the embed URL hostname.

    ``on_fallback(event, host, exc)`` is invoked before each fallback attempt;
    event keys match ``CONTENT["progress"]`` so the caller can render them.
    """
    embed_url = _normalize_url(embed_url)
    host = urlparse(embed_url).netloc.lower()

    _ytdlp_kw = {"cookies_file": cookies_file, "cookies_browser": cookies_browser}

    rule = HOST_RULES.get(host)

    if rule is not None and rule.mode == "unsupported":
        reason = rule.reason or "unsupported_host"
        error_cls = JavaScriptRequiredError if reason == "js_only_host" else UnsupportedHostError
        raise error_cls(EXCEPTIONS["extract"][reason].format(host=host))

    if rule is not None and rule.mode == "ytdlp":
        try:
            return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
        except Exception as exc:
            raise NoStreamError(EXCEPTIONS["extract"]["ytdlp_failed"].format(embed_url=repr(embed_url), exc=exc)) from exc

    if rule is not None:
        resolver = rule.resolver or jwplayer.resolve
        try:
            return resolver(embed_url, referer)
        except Exception as exc:
            if on_fallback:
                on_fallback("extractor_fallback", host, exc)
            try:
                return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
            except Exception:
                raise NoStreamError(EXCEPTIONS["extract"]["all_failed"].format(embed_url=repr(embed_url))) from exc

    # Unknown host — try JWPlayer first (covers most embed-site patterns),
    # then fall back to yt-dlp (1500+ supported sites).
    try:
        return jwplayer.resolve(embed_url, referer)
    except Exception as exc:
        if on_fallback:
            on_fallback("jwplayer_fallback", host, exc)

    try:
        return ytdlp_resolver.resolve(embed_url, referer, **_ytdlp_kw)
    except Exception as exc:
        raise NoStreamError(EXCEPTIONS["extract"]["all_failed_exc"].format(embed_url=repr(embed_url), exc=exc)) from exc
