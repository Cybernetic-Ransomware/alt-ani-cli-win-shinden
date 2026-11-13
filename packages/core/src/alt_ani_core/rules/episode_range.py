import contextlib

from alt_ani_core.models import EpisodeRow


def parse_range(range_str: str, episodes: list[EpisodeRow]) -> list[EpisodeRow] | None:
    """Convert a range string to an ordered list of episodes; None when the format is not recognised.

    Supported formats:
      "5"     → single episode 5
      "1-5"   → episodes 1 through 5 (inclusive)
      "-1"    → last available episode
      "1 5 7" → episodes 1, 5 and 7 (space-separated)
    """
    s = range_str.strip()

    if s == "-1":
        return [episodes[-1]] if episodes else []

    # Range: start-end (but not a negative number — those are handled above)
    if "-" in s and not s.startswith("-"):
        parts = s.split("-", 1)
        try:
            start, end = float(parts[0]), float(parts[1])
            return [ep for ep in episodes if start <= ep.number <= end]
        except ValueError:
            pass

    # Space-separated multi-select
    if " " in s:
        targets = set()
        for p in s.split():
            with contextlib.suppress(ValueError):
                targets.add(float(p))
        return [ep for ep in episodes if ep.number in targets]

    try:
        num = float(s)
        return [ep for ep in episodes if ep.number == num]
    except ValueError:
        return None
