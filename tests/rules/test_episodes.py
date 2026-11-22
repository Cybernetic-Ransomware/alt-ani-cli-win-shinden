import pytest

from alt_ani_core.models import EpisodeRow, PlayerEntry
from alt_ani_core.rules.criteria import NoMatch, item_matches, no_match_reason
from alt_ani_core.rules.episodes import later_available, next_episode, select_episodes
from alt_ani_core.state import DownloadCriteria, OfflineItemSpec, QualityCriterion, StorageLocator

LISTING = [EpisodeRow(n, f"Ep {n:g}", f"https://shinden.pl/e/{n:g}") for n in (1, 2, 3, 4, 5)]


def entry(audio: str, subs: str) -> PlayerEntry:
    return PlayerEntry(online_id="1", player="host", lang_audio=audio, lang_subs=subs)


@pytest.mark.unit
class TestNextEpisode:
    def test_next_in_known_order(self):
        assert next_episode([3, 1, 2], 1) == 2

    def test_fractional_episode_counts(self):
        assert next_episode([1, 1.5, 2], 1) == 1.5

    def test_none_after_the_last(self):
        assert next_episode([1, 2], 2) is None

    def test_later_copy_skips_the_excluded_next(self):
        assert later_available([2, 4], 1, exclude=2) == 4
        assert later_available([1], 1) is None


@pytest.mark.unit
class TestSelectEpisodes:
    def test_explicit_numbers(self):
        assert [e.number for e in select_episodes(LISTING, "episodes", numbers=[4, 2])] == [2, 4]

    def test_range(self):
        assert [e.number for e in select_episodes(LISTING, "range", range_text="2-4")] == [2, 3, 4]

    def test_unknown_range_is_none(self):
        assert select_episodes(LISTING, "range", range_text="abc") is None

    def test_unwatched_includes_gaps_before_a_later_watched_episode(self):
        chosen = select_episodes(LISTING, "unwatched", watched=[1, 3])
        assert [e.number for e in chosen] == [2, 4, 5]

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            select_episodes(LISTING, "bogus")


@pytest.mark.unit
class TestNoMatchReason:
    def test_audio_first(self):
        assert no_match_reason([entry("jp", "pl")], DownloadCriteria(audio="pl")) == NoMatch("audio")

    def test_subs_only_among_matching_audio(self):
        entries = [entry("jp", "en"), entry("pl", "pl")]
        assert no_match_reason(entries, DownloadCriteria(audio="jp", subs="pl")) == NoMatch("subs")

    def test_match(self):
        assert no_match_reason([entry("jp", "pl")], DownloadCriteria(audio="jp", subs="pl")) is None


@pytest.mark.unit
class TestItemMatches:
    def spec(self, **kw) -> OfflineItemSpec:
        return OfflineItemSpec(locator=StorageLocator("fs", "a.mp4"), **kw)

    def test_actual_languages_decide(self):
        assert item_matches(self.spec(audio="JP", subs="pl", height=720), DownloadCriteria(audio="jp", subs="pl"))
        assert not item_matches(self.spec(audio="jp", subs="en"), DownloadCriteria(subs="pl"))

    def test_unknown_language_never_satisfies_a_criterion(self):
        assert not item_matches(self.spec(audio=None), DownloadCriteria(audio="jp"))
        assert item_matches(self.spec(audio=None), DownloadCriteria())

    def test_quality_is_an_upper_limit_and_unknown_height_fails_it(self):
        limit = DownloadCriteria(quality=QualityCriterion(720))
        assert item_matches(self.spec(height=480), limit)
        assert not item_matches(self.spec(height=1080), limit)
        assert not item_matches(self.spec(height=None), limit)
        assert item_matches(self.spec(height=None), DownloadCriteria())
