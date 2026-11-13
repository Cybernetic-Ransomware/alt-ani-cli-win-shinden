import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from alt_ani_core.cancel import NEVER, CancelToken
from alt_ani_core.config import ExtractOptions
from alt_ani_core.errors import JavaScriptRequiredError, NoStreamError, UnsupportedHostError
from alt_ani_core.extract import (
    dood,
    flyf,
    jwplayer,
    lycoris,
    mp4upload,
    playmate,
    streamtape,
    uqload,
    vidara,
    ytdlp_resolver,
)
from alt_ani_core.extract.common import Stream
from alt_ani_core.messages import MESSAGES as EXCEPTIONS
from alt_ani_core.models import ExtractionFailure
from alt_ani_core.redaction import redact_text
from alt_ani_core.rules.failure import classify_exception, http_status_of
from alt_ani_core.transport.api import Transport

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
    resolver: Callable[[str, str, Transport], Stream] | None = None
    reason: str | None = None


HOST_RULES: dict[str, HostRule] = {
    # pure JS SPA — no video URL in initial HTML, requires a browser;
    # fail fast instead of wasting 5 s antibot + HTTP roundtrip
    "bysesukior.com": HostRule("unsupported", reason="js_only_host"),
    "www.bysesukior.com": HostRule("unsupported", reason="js_only_host"),
    "voe.sx": HostRule("unsupported", reason="js_only_host"),
    "www.voe.sx": HostRule("unsupported", reason="js_only_host"),
    # end-to-end encrypted (AES-CTR, key in URL fragment) — no player or extractor can decrypt
    "mega.nz": HostRule("unsupported", reason="encrypted_host"),
    "www.mega.nz": HostRule("unsupported", reason="encrypted_host"),
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
    "dood.yt": HostRule("custom", dood.resolve),
    "dooood.com": HostRule("custom", dood.resolve),
    "ds2play.com": HostRule("custom", dood.resolve),
    # vidara / viewdara — same host-agnostic API, base URL derived from the embed URL itself
    "vidara.to": HostRule("custom", vidara.resolve),
    "www.vidara.to": HostRule("custom", vidara.resolve),
    "viewdara.com": HostRule("custom", vidara.resolve),
    "www.viewdara.com": HostRule("custom", vidara.resolve),
    # vidawra/vidwara/vidvara — same /api/stream protocol as vidara, confirmed by live replay
    "vidawra.cc": HostRule("custom", vidara.resolve),
    "vidawra.co": HostRule("custom", vidara.resolve),
    "vidwara.site": HostRule("custom", vidara.resolve),
    "vidvara.biz": HostRule("custom", vidara.resolve),
    # morningmarkets — cosmetic JWPlayer skin, but the actual source comes from vidara's
    # /api/stream protocol, confirmed by live replay
    "morningmarkets.art": HostRule("custom", vidara.resolve),
    "morningmarkets.fit": HostRule("custom", vidara.resolve),
    # iosbgaigo — JWPlayer source is set from a POST /api/stream response at runtime, confirmed by live replay
    "iosbgaigo.com": HostRule("custom", vidara.resolve),
    "www.iosbgaigo.com": HostRule("custom", vidara.resolve),
    # lycoris
    "lycoris.cafe": HostRule("custom", lycoris.resolve),
    "www.lycoris.cafe": HostRule("custom", lycoris.resolve),
    # playmate
    "playmate.to": HostRule("custom", playmate.resolve),
    "www.playmate.to": HostRule("custom", playmate.resolve),
    # uqload
    "uqload.is": HostRule("custom", uqload.resolve),
    "www.uqload.is": HostRule("custom", uqload.resolve),
    # flyf
    "flyf.lat": HostRule("custom", flyf.resolve),
    "www.flyf.lat": HostRule("custom", flyf.resolve),
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
    "morencius.com": HostRule("jwplayer"),
    "www.morencius.com": HostRule("jwplayer"),
}


def resolver_family(host: str) -> str:
    """Diagnostics-only family label for a hostname — never influences health decisions."""
    rule = HOST_RULES.get(host) or HOST_RULES.get(f"www.{host}")
    if rule is None:
        return "generic"
    if rule.mode == "unsupported":
        return "unsupported"
    if rule.mode == "ytdlp":
        return "ytdlp"
    if rule.resolver is None:
        return "jwplayer"
    return rule.resolver.__module__.rsplit(".", 1)[-1]


def _exc_text(exc: Exception, embed_url: str, host: str) -> str:
    """Format an exception for user-facing messages, replacing the embed URL with its host."""
    text = f"{type(exc).__name__}: {exc}".replace(repr(embed_url), host).replace(embed_url, host)
    return redact_text(text)


def _failed(
    err: NoStreamError,
    *,
    layer: str,
    cause: BaseException,
    fallback_layer: str | None = None,
    fallback_cause: BaseException | None = None,
) -> NoStreamError:
    err.failure = ExtractionFailure(
        layer=layer,
        category=classify_exception(cause),
        http_status=http_status_of(cause),
        used_fallback=fallback_cause is not None,
        fallback_layer=fallback_layer,
        fallback_category=classify_exception(fallback_cause) if fallback_cause is not None else None,
        fallback_http_status=http_status_of(fallback_cause) if fallback_cause is not None else None,
    )
    return err


def resolve(
    embed_url: str,
    referer: str,
    *,
    transport: Transport,
    options: ExtractOptions | None = None,
    on_fallback: Callable[[str, str, str], None] | None = None,
    cancel: CancelToken = NEVER,
    check: Callable[[], None] | None = None,
) -> Stream:
    """Dispatch to the right extractor based on the embed URL hostname.

    ``on_fallback(event, host, exc_text)`` is invoked before each fallback attempt.
    ``check`` (raises EngineInterrupt once stale) runs with ``cancel`` before every attempt, so no stale fallback starts.
    """

    def ensure_live() -> None:
        cancel.raise_if_cancelled()
        if check is not None:
            check()

    ensure_live()
    embed_url = _normalize_url(embed_url)
    host = urlparse(embed_url).netloc.lower()

    rule = HOST_RULES.get(host)

    if rule is not None and rule.mode == "unsupported":
        reason = rule.reason or "unsupported_host"
        error_cls = JavaScriptRequiredError if reason == "js_only_host" else UnsupportedHostError
        err = error_cls(EXCEPTIONS["extract"][reason].format(host=host))
        raise _failed(err, layer="unsupported", cause=err)

    if rule is not None and rule.mode == "ytdlp":
        try:
            return ytdlp_resolver.resolve(embed_url, referer, options)
        except Exception as exc:
            err = NoStreamError(EXCEPTIONS["extract"]["ytdlp_failed"].format(host=host, exc=_exc_text(exc, embed_url, host)))
            raise _failed(err, layer="ytdlp", cause=exc) from exc

    if rule is not None:
        resolver = rule.resolver or jwplayer.resolve
        layer = "custom" if rule.resolver else "jwplayer"
        try:
            return resolver(embed_url, referer, transport)
        except Exception as exc:
            ensure_live()
            if on_fallback:
                on_fallback("extractor_fallback", host, _exc_text(exc, embed_url, host))
            # the callback runs host code that may cancel or retire the operation
            ensure_live()
            try:
                return ytdlp_resolver.resolve(embed_url, referer, options)
            except Exception as fallback_exc:
                err = NoStreamError(EXCEPTIONS["extract"]["all_failed"].format(host=host))
                raise _failed(err, layer=layer, cause=exc, fallback_layer="ytdlp", fallback_cause=fallback_exc) from exc

    # Unknown host — try JWPlayer first (covers most embed-site patterns),
    # then fall back to yt-dlp (1500+ supported sites).
    jwplayer_exc: Exception | None = None
    try:
        return jwplayer.resolve(embed_url, referer, transport)
    except Exception as exc:
        jwplayer_exc = exc
        ensure_live()
        if on_fallback:
            on_fallback("jwplayer_fallback", host, _exc_text(exc, embed_url, host))
        ensure_live()

    try:
        return ytdlp_resolver.resolve(embed_url, referer, options)
    except Exception as exc:
        err = NoStreamError(EXCEPTIONS["extract"]["all_failed_exc"].format(host=host, exc=_exc_text(exc, embed_url, host)))
        raise _failed(err, layer="jwplayer", cause=jwplayer_exc or exc, fallback_layer="ytdlp", fallback_cause=exc) from exc
