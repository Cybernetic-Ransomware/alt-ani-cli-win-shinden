import pytest
from alt_ani_core.models import EpisodeRow
from alt_ani_core.rules.episode_range import parse_range


def _ep(n: float) -> EpisodeRow:
    return EpisodeRow(number=n, title=f"Ep {n:g}", url=f"https://shinden.pl/ep/{n:g}")


EPS = [_ep(float(i)) for i in range(1, 14)]


@pytest.mark.unit
class TestParseRange:
    def test_single(self):
        result = parse_range("5", EPS)
        assert [ep.number for ep in result] == [5.0]

    def test_range(self):
        result = parse_range("1-3", EPS)
        assert [ep.number for ep in result] == [1.0, 2.0, 3.0]

    def test_last(self):
        result = parse_range("-1", EPS)
        assert result == [EPS[-1]]

    def test_space_separated(self):
        result = parse_range("1 5 7", EPS)
        assert {ep.number for ep in result} == {1.0, 5.0, 7.0}

    def test_nonexistent_episode(self):
        result = parse_range("99", EPS)
        assert result == []

    def test_empty_list(self):
        result = parse_range("-1", [])
        assert result == []

    def test_surrounding_whitespace_is_ignored(self):
        assert parse_range("  5 ", EPS) == [EPS[4]]

    @pytest.mark.parametrize("value", ["abc", "1-x", ""])
    def test_unrecognised_format_returns_none(self, value):
        assert parse_range(value, EPS) is None

    def test_recognised_format_without_match_is_empty_not_none(self):
        assert parse_range("50-60", EPS) == []
