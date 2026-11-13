"""Which episodes a selection means and which one comes next; order is the known listing, never a guess."""

from collections.abc import Collection, Iterable, Sequence

from alt_ani_core.models import EpisodeRow
from alt_ani_core.rules.episode_range import parse_range

MODES = ("episodes", "range", "unwatched")


def next_episode(numbers: Iterable[float], current: float) -> float | None:
    """The first known episode after ``current``; None when the listing knows none."""
    later = [n for n in set(map(float, numbers)) if n > current]
    return min(later) if later else None


def later_available(numbers_with_copies: Iterable[float], current: float, *, exclude: float | None = None) -> float | None:
    """The nearest later episode with a local copy; a separate proposal, never presented as the next episode."""
    later = [n for n in set(map(float, numbers_with_copies)) if n > current and n != exclude]
    return min(later) if later else None


def select_episodes(
    listing: Sequence[EpisodeRow],
    mode: str,
    *,
    numbers: Collection[float] = (),
    range_text: str | None = None,
    watched: Collection[float] = (),
) -> list[EpisodeRow] | None:
    """None when the range is not understood; "unwatched" includes gaps before a later watched episode."""
    if mode == "episodes":
        wanted = {float(n) for n in numbers}
        return [ep for ep in listing if ep.number in wanted]
    if mode == "range":
        return None if range_text is None else parse_range(range_text, list(listing))
    if mode == "unwatched":
        seen = {float(n) for n in watched}
        return [ep for ep in listing if ep.number not in seen]
    raise ValueError(f"unknown selection mode {mode!r}")
