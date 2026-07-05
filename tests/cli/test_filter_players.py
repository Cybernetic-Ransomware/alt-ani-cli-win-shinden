"""Tests for _filter_players — strict AND filter with unmatched reporting."""

import pytest

from alt_ani_cli.cli import _filter_players
from alt_ani_cli.shinden.models import PlayerEntry


def _p(player: str, lang_audio: str = "jp", lang_subs: str = "pl") -> PlayerEntry:
    return PlayerEntry(online_id="x", player=player, lang_audio=lang_audio, lang_subs=lang_subs)


_PL_DUB = _p("CDA", lang_audio="pl", lang_subs="none")
_JP_SUB_PL = _p("Sibnet", lang_audio="jp", lang_subs="pl")
_JP_SUB_EN = _p("Mp4upload", lang_audio="jp", lang_subs="en")
_ALL = [_PL_DUB, _JP_SUB_PL, _JP_SUB_EN]


@pytest.mark.unit
class TestFilterPlayers:
    def test_no_filters_returns_full_list(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs=None, player_name=None)
        assert filtered == _ALL
        assert unmatched == []

    def test_matching_lang_narrows_list(self):
        filtered, unmatched = _filter_players(_ALL, lang="pl", subs=None, player_name=None)
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_matching_subs_narrows_list(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs="en", player_name=None)
        assert filtered == [_JP_SUB_EN]
        assert unmatched == []

    def test_matching_player_name_narrows_list(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs=None, player_name="CDA")
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_player_name_is_case_insensitive(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs=None, player_name="cda")
        assert filtered == [_PL_DUB]
        assert unmatched == []

    def test_and_combination_matches_correctly(self):
        filtered, unmatched = _filter_players(_ALL, lang="jp", subs="pl", player_name=None)
        assert filtered == [_JP_SUB_PL]
        assert unmatched == []

    def test_failing_lang_returns_empty_with_label(self):
        filtered, unmatched = _filter_players(_ALL, lang="xx", subs=None, player_name=None)
        assert filtered == []
        assert unmatched == ["--lang=xx"]

    def test_failing_subs_returns_empty_with_label(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs="xx", player_name=None)
        assert filtered == []
        assert unmatched == ["--subs=xx"]

    def test_failing_player_name_returns_empty_with_label(self):
        filtered, unmatched = _filter_players(_ALL, lang=None, subs=None, player_name="StreamTape")
        assert filtered == []
        assert unmatched == ["--player-name=StreamTape"]

    def test_all_criteria_failing_reports_all_labels(self):
        filtered, unmatched = _filter_players(_ALL, lang="xx", subs="yy", player_name="No")
        assert filtered == []
        assert "--lang=xx" in unmatched
        assert "--subs=yy" in unmatched
        assert "--player-name=No" in unmatched

    def test_one_failing_criterion_does_not_narrow_pool_for_remaining(self):
        # player_name fails → pool stays full → subs="pl" still finds two
        filtered, unmatched = _filter_players(_ALL, lang=None, subs="pl", player_name="NoSuch")
        assert filtered == []
        assert "--player-name=NoSuch" in unmatched
        assert "--subs=pl" not in unmatched

    def test_intersection_empty_reports_criterion_that_emptied_pool(self):
        # lang="pl" → [_PL_DUB]; subs="en" has no match in [_PL_DUB]
        filtered, unmatched = _filter_players(_ALL, lang="pl", subs="en", player_name=None)
        assert filtered == []
        assert "--subs=en" in unmatched
        assert "--lang=pl" not in unmatched
