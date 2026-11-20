"""Parsers for objects several methods share; every failure is an ``InputError`` naming the field."""

from alt_ani_bridge.schema import InputError, Reader
from alt_ani_core.config import AccessConfig
from alt_ani_core.models import EpisodeRow, SeriesRef
from alt_ani_core.rules.criteria import criteria_for
from alt_ani_core.state import (
    AlternativesPolicy,
    DownloadCriteria,
    EpisodeKey,
    PlaybackSession,
    Preferences,
    QualityCriterion,
    VariantIdentity,
)

SERIES_FIELDS = ("id", "slug", "title", "url")
EPISODE_FIELDS = ("number", "title", "url")
CRITERIA_FIELDS = ("audio", "subs", "quality")
VARIANT_FIELDS = ("label", "height", "protocol", "container")
PLAYBACK_FIELDS = ("id", "series_id", "number")


def series(r: Reader, name: str = "series") -> SeriesRef:
    s = r.obj(name, SERIES_FIELDS)
    return SeriesRef(id=s.text("id"), slug=s.text("slug", "", nonempty=False), title=s.text("title"), url=s.text("url"))


def episode_row(e: Reader) -> EpisodeRow:
    return EpisodeRow(number=e.number("number"), title=e.text("title", "", nonempty=False), url=e.text("url"))


def episode(r: Reader, name: str = "episode") -> EpisodeRow:
    return episode_row(r.obj(name, EPISODE_FIELDS))


def episodes(r: Reader, name: str = "episodes") -> list[EpisodeRow]:
    return [episode_row(e) for e in r.objs(name, EPISODE_FIELDS, "episodes")]


def access(r: Reader, name: str = "access", *, required: bool = True) -> AccessConfig | None:
    a = (
        r.obj(name, ("flaresolverr_url", "use_flaresolverr"))
        if required
        else r.obj(name, ("flaresolverr_url", "use_flaresolverr"), None)
    )
    if a is None:
        return None
    return AccessConfig(
        flaresolverr_url=a.text("flaresolverr_url", None, nullable=True) or None,
        use_flaresolverr=a.flag("use_flaresolverr", True),
    )


def quality(text: str, field: str) -> QualityCriterion:
    try:
        return QualityCriterion.parse(text)
    except ValueError:
        raise InputError(field, "unknown_value", expected="best or max:<height>") from None


def criteria_overrides(r: Reader, prefs: Preferences, name: str = "criteria") -> DownloadCriteria:
    """The preferences with a one-off change; a language is never widened, so an empty value is refused."""
    c = r.obj(name, CRITERIA_FIELDS, None)
    if c is None:
        return criteria_for(prefs)
    q = c.text("quality", None)
    try:
        return criteria_for(
            prefs,
            audio=c.text("audio", None),
            subs=c.text("subs", None),
            quality=None if q is None else quality(q, c.field("quality")),
        )
    except ValueError:
        raise InputError(r.field(name), "invalid_criteria") from None


def full_criteria(r: Reader, name: str = "criteria") -> DownloadCriteria:
    """A snapshot from a preview: every field present, nothing filled in from the preferences."""
    c = r.obj(name, CRITERIA_FIELDS)
    try:
        return DownloadCriteria(
            audio=c.text("audio"), subs=c.text("subs"), quality=quality(c.text("quality"), c.field("quality"))
        )
    except ValueError:
        raise InputError(r.field(name), "invalid_criteria") from None


def alternatives(r: Reader, prefs: Preferences | None, name: str = "alternatives") -> AlternativesPolicy:
    if prefs is None:
        return r.enum(name, AlternativesPolicy)
    default = AlternativesPolicy.COMPATIBLE if prefs.auto_alternatives else AlternativesPolicy.OFF
    return r.enum(name, AlternativesPolicy, default)


def variant(r: Reader, name: str = "variant") -> VariantIdentity | None:
    v = r.obj(name, VARIANT_FIELDS, None)
    if v is None:
        return None
    return VariantIdentity(
        label=v.text("label", nonempty=False),
        height=v.integer("height", None, minimum=1, nullable=True),
        protocol=v.text("protocol"),
        container=v.text("container", None, nullable=True),
    )


def key(r: Reader, series_field: str = "series_id", number_field: str = "number") -> EpisodeKey:
    return EpisodeKey(r.text(series_field), r.number(number_field))


def playback_session(r: Reader, name: str = "playback_session") -> PlaybackSession:
    p = r.obj(name, PLAYBACK_FIELDS)
    return PlaybackSession(EpisodeKey(p.text("series_id"), p.number("number")), p.text("id"))
