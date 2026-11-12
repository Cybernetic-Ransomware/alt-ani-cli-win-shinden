"""Download and playback criteria: explicit matching that never weakens a criterion to find something."""

from dataclasses import dataclass, replace

from alt_ani_core.models import PlayerEntry, Stream, StreamVariant
from alt_ani_core.rules.pin import player_fingerprint
from alt_ani_core.state import (
    ANY,
    DownloadCriteria,
    PartialTransfer,
    Preferences,
    QualityCriterion,
    ResourceValidator,
    SourceIdentity,
    VariantIdentity,
    normalize_language,
)


@dataclass(frozen=True)
class NoMatch:
    reason: str


def criteria_for(
    prefs: Preferences, *, audio: str | None = None, subs: str | None = None, quality: QualityCriterion | None = None
) -> DownloadCriteria:
    """The preferences with a one-off change; the result is copied into a job and never follows later edits."""
    return DownloadCriteria(
        audio=prefs.audio if audio is None else audio,
        subs=prefs.subs if subs is None else subs,
        quality=prefs.quality if quality is None else quality,
    )


def audio_matches(entry: PlayerEntry, wanted: str) -> bool:
    return wanted == ANY or normalize_language(entry.lang_audio) == wanted


def subs_match(entry: PlayerEntry, wanted: str) -> bool:
    """``none`` needs a source that declares no subtitles; an empty declaration is unknown, not absent."""
    if wanted == ANY:
        return True
    return normalize_language(entry.lang_subs) == wanted


def match_source(entry: PlayerEntry, criteria: DownloadCriteria) -> bool:
    return audio_matches(entry, criteria.audio) and subs_match(entry, criteria.subs)


def _known_height(variant: StreamVariant) -> int | None:
    return variant.height or None


def match_variant(stream: Stream, quality: QualityCriterion) -> StreamVariant | NoMatch:
    """Best known height within the limit; best available without one. Never falls back past the limit."""
    known = [v for v in stream.variants if _known_height(v) is not None]
    if quality.max_height is None:
        return max(known, key=lambda v: v.height) if known else stream.default
    fitting = [v for v in known if v.height <= quality.max_height]
    if not fitting:
        return NoMatch("quality")
    return max(fitting, key=lambda v: v.height)


def source_identity(entry: PlayerEntry) -> SourceIdentity:
    fp = player_fingerprint(entry)
    return SourceIdentity(
        online_id=entry.online_id,
        player=fp.player,
        lang_audio=fp.lang_audio,
        lang_subs=fp.lang_subs,
        max_res=fp.max_res,
        subs_author=fp.subs_author,
        source=fp.source,
    )


def variant_identity(variant: StreamVariant) -> VariantIdentity:
    return VariantIdentity(
        label=variant.label,
        height=_known_height(variant),
        protocol=variant.protocol.value,
        container=variant.container,
    )


def same_player(a: SourceIdentity, b: SourceIdentity) -> bool:
    """Same host entry regardless of the per-episode online id."""
    return replace(a, online_id="") == replace(b, online_id="")


def resume_candidate(partial: PartialTransfer | None, source: SourceIdentity, variant: VariantIdentity) -> bool:
    """Only worth checking: the executor still has to confirm the resource with ``same_resource`` or start over."""
    return partial is not None and partial.validator is not None and partial.source == source and partial.variant == variant


def same_resource(saved: ResourceValidator | None, observed: ResourceValidator | None) -> bool:
    """False whenever either side lacks evidence; an unverifiable partial is restarted, never spliced."""
    if saved is None or observed is None or not saved.fields:
        return False
    return saved == observed
