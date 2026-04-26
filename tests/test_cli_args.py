import pytest
from alt_ani_cli.cli import _build_parser, _parse_range
from alt_ani_cli.shinden.models import EpisodeRow


def _ep(n: float) -> EpisodeRow:
    return EpisodeRow(number=n, title=f"Ep {n:g}", url=f"https://shinden.pl/ep/{n:g}")


EPS = [_ep(float(i)) for i in range(1, 14)]


class TestArgParser:
    def test_url_flag(self):
        p = _build_parser()
        args = p.parse_args(["--url", "https://shinden.pl/series/123-slug"])
        assert args.url == "https://shinden.pl/series/123-slug"

    def test_episode_short(self):
        p = _build_parser()
        args = p.parse_args(["-e", "5", "--url", "u"])
        assert args.episode == "5"

    def test_episode_long(self):
        p = _build_parser()
        args = p.parse_args(["--episode", "1-5", "--url", "u"])
        assert args.episode == "1-5"

    def test_vlc_flag(self):
        p = _build_parser()
        args = p.parse_args(["--vlc", "--url", "u"])
        assert args.vlc is True

    def test_debug_flag(self):
        p = _build_parser()
        args = p.parse_args(["--debug", "--url", "u"])
        assert args.debug is True

    def test_query_positional(self):
        p = _build_parser()
        args = p.parse_args(["fate", "strange", "fake"])
        assert args.query == ["fate", "strange", "fake"]

    def test_flags_before_query(self):
        p = _build_parser()
        a1 = p.parse_args(["-d", "-e", "5", "fate", "strange"])
        a2 = p.parse_args(["fate", "strange", "-d", "-e", "5"])
        assert a1.download == a2.download
        assert a1.episode == a2.episode
        assert a1.query == a2.query


class TestParseRange:
    def test_single(self):
        result = _parse_range("5", EPS)
        assert [ep.number for ep in result] == [5.0]

    def test_range(self):
        result = _parse_range("1-3", EPS)
        assert [ep.number for ep in result] == [1.0, 2.0, 3.0]

    def test_last(self):
        result = _parse_range("-1", EPS)
        assert result == [EPS[-1]]

    def test_space_separated(self):
        result = _parse_range("1 5 7", EPS)
        assert {ep.number for ep in result} == {1.0, 5.0, 7.0}

    def test_nonexistent_episode(self):
        result = _parse_range("99", EPS)
        assert result == []

    def test_empty_list(self):
        result = _parse_range("-1", [])
        assert result == []
