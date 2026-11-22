"""Strict AND filter with unmatched reporting."""

import pytest

from alt_ani_core.models import PlayerEntry, SourceFilter
from alt_ani_core.rules.filtering import filter_players


def _p(player: str, lang_audio: str = "jp", lang_subs: str = "pl") -> PlayerEntry:
    return PlayerEntry(online_id="x", player=player, lang_audio=lang_audio, lang_subs=lang_subs)


_PL_DUB = _p("CDA", lang_audio="pl", lang_subs="none")
_JP_SUB_PL = _p("Sibnet", lang_audio="jp", lang_subs="pl")
_JP_SUB_EN = _p("Mp4upload", lang_audio="jp", lang_subs="en")
_ALL = [_PL_DUB, _JP_SUB_PL, _JP_SUB_EN]


def _filter(lang=None, subs=None, player_name=None):
    return filter_players(_ALL, SourceFilter(lang=lang, subs=subs, player_name=player_name))


@pytest.mark.unit
class TestFilterPlayers:
    def test_no_filters_returns_full_list(self):
        filtered, unmatched = _filter()
        assert filtered == _ALL
        assert unmatched == []

    def test_result_is_a_copy_of_the_input(self):
        filtered, _ = _filter()
        assert filtered is not _ALL

    def test_matching_lang_narrows_list(self):
        filtered, unmatched = _filter(lang="pl")
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_matching_subs_narrows_list(self):
        filtered, unmatched = _filter(subs="en")
        assert filtered == [_JP_SUB_EN]
        assert unmatched == []

    def test_matching_player_name_narrows_list(self):
        filtered, unmatched = _filter(player_name="CDA")
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_player_name_is_case_insensitive(self):
        filtered, unmatched = _filter(player_name="cda")
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_and_combination_matches_correctly(self):
        filtered, unmatched = _filter(lang="jp", subs="pl")
        assert filtered == [_JP_SUB_PL]
        assert unmatched == []

    def test_failing_lang_returns_empty_with_label(self):
        filtered, unmatched = _filter(lang="xx")
        assert filtered == []
        assert unmatched == ["--lang=xx"]

    def test_failing_subs_returns_empty_with_label(self):
        filtered, unmatched = _filter(subs="xx")
        assert filtered == []
        assert unmatched == ["--subs=xx"]

    def test_failing_player_name_returns_empty_with_label(self):
        filtered, unmatched = _filter(player_name="StreamTape")
        assert filtered == []
        assert unmatched == ["--player-name=StreamTape"]

    def test_all_criteria_failing_reports_all_labels(self):
        filtered, unmatched = _filter(lang="xx", subs="yy", player_name="No")
        assert filtered == []
        assert "--lang=xx" in unmatched
        assert "--subs=yy" in unmatched
        assert "--player-name=No" in unmatched

    def test_one_failing_criterion_does_not_narrow_pool_for_remaining(self):
        # player_name fails → pool stays full → subs="pl" still finds a match
        filtered, unmatched = _filter(subs="pl", player_name="NoSuch")
        assert filtered == []
        assert "--player-name=NoSuch" in unmatched
        assert "--subs=pl" not in unmatched

    def test_intersection_empty_reports_criterion_that_emptied_pool(self):
        # lang="pl" → [_PL_DUB]; subs="en" has no match in [_PL_DUB]
        filtered, unmatched = _filter(lang="pl", subs="en")
        assert filtered == []
        assert "--subs=en" in unmatched
        assert "--lang=pl" not in unmatched


@pytest.mark.unit
class TestSourceFilter:
    def test_inactive_without_criteria(self):
        assert SourceFilter().active is False

    @pytest.mark.parametrize("criteria", [{"lang": "pl"}, {"subs": "en"}, {"player_name": "CDA"}])
    def test_any_criterion_makes_it_active(self, criteria):
        assert SourceFilter(**criteria).active is True
