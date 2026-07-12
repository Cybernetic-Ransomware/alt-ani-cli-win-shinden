"""Tests for shinden/episode.py — parse_players and sort_players."""

import pytest

from alt_ani_cli.shinden.episode import parse_players, sort_players
from alt_ani_cli.shinden.models import PlayerEntry


def _p(player, lang_audio, lang_subs, max_res=None, date_added=None):
    return PlayerEntry(
        online_id="x",
        player=player,
        lang_audio=lang_audio,
        lang_subs=lang_subs,
        max_res=max_res,
        date_added=date_added,
    )


@pytest.mark.unit
class TestParsePlayers:
    def test_valid_json(self, episode_players_html):
        players = parse_players(episode_players_html)
        cda = next((p for p in players if p.player == "CDA"), None)
        assert cda is not None
        assert cda.online_id == "67890"
        assert cda.lang_audio == "jp"
        assert cda.lang_subs == "pl"
        assert cda.max_res == "1080p"

    def test_subs_author_and_source_parsed(self, episode_players_html):
        players = parse_players(episode_players_html)
        cda = next(p for p in players if p.player == "CDA")
        assert cda.subs_author == "Mioro-Subs"
        assert cda.source == "https://miorosubs.com/"

    def test_subs_author_and_source_null(self, episode_players_html):
        players = parse_players(episode_players_html)
        filemoon = next(p for p in players if p.player == "Filemoon")
        assert filemoon.subs_author is None
        assert filemoon.source is None

    def test_subs_author_and_source_missing_keys(self):
        html = """<a data-episode="{&quot;online_id&quot;:&quot;abc&quot;,&quot;player&quot;:&quot;CDA&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;}">x</a>"""
        players = parse_players(html)
        assert players[0].subs_author is None
        assert players[0].source is None

    def test_subs_author_empty_string_is_none(self):
        html = (
            '<a data-episode="{&quot;online_id&quot;:&quot;abc&quot;,&quot;player&quot;:&quot;CDA&quot;,'
            '&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;,'
            '&quot;subs_author&quot;:&quot;  &quot;,&quot;source&quot;:&quot;&quot;}">x</a>'
        )
        players = parse_players(html)
        assert players[0].subs_author is None
        assert players[0].source is None

    def test_html_unescape(self):
        html = """<a data-episode="{&quot;online_id&quot;:&quot;abc&quot;,&quot;player&quot;:&quot;Mp4upload&quot;,&quot;lang_audio&quot;:&quot;pl&quot;,&quot;lang_subs&quot;:&quot;&quot;}">x</a>"""
        players = parse_players(html)
        assert len(players) == 1
        assert players[0].online_id == "abc"
        assert players[0].player == "Mp4upload"
        assert players[0].max_res is None

    def test_empty_html(self):
        assert parse_players("<html><body></body></html>") == []

    def test_skips_missing_fields(self):
        html = '<a data-episode="{&quot;player&quot;:&quot;CDA&quot;}">x</a>'
        assert parse_players(html) == []

    def test_date_parsed_from_table_row(self):
        html = """<html><body>
        <table><tbody>
          <tr>
            <td><a data-episode="{&quot;online_id&quot;:&quot;1&quot;,&quot;player&quot;:&quot;CDA&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;}">CDA</a></td>
            <td class="ep-online-added">2024-03-15</td>
          </tr>
        </tbody></table>
        </body></html>"""
        players = parse_players(html)
        assert len(players) == 1
        assert players[0].date_added == "2024-03-15"

    def test_date_none_when_no_column(self):
        html = """<html><body>
        <table><tbody>
          <tr>
            <td><a data-episode="{&quot;online_id&quot;:&quot;2&quot;,&quot;player&quot;:&quot;Sibnet&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;}">Sibnet</a></td>
          </tr>
        </tbody></table>
        </body></html>"""
        players = parse_players(html)
        assert players[0].date_added is None


@pytest.mark.unit
class TestSortPlayers:
    def test_audio_pl_first(self):
        players = [_p("Sibnet", "jp", "pl"), _p("CDA", "pl", "pl")]
        assert sort_players(players)[0].player == "CDA"

    def test_audio_then_subs(self):
        players = [_p("A", "jp", "pl"), _p("B", "jp", "en"), _p("C", "jp", "")]
        assert [p.player for p in sort_players(players)] == ["A", "B", "C"]

    def test_audio_then_res(self):
        players = [_p("A", "jp", "pl", "720p"), _p("B", "jp", "pl", "1080p")]
        assert sort_players(players)[0].player == "B"

    def test_date_desc_tiebreaker(self):
        players = [_p("A", "jp", "pl", "720p", "2024-01-01"), _p("B", "jp", "pl", "720p", "2024-06-15")]
        assert sort_players(players)[0].player == "B"

    def test_none_date_last(self):
        players = [_p("A", "jp", "pl", "720p", None), _p("B", "jp", "pl", "720p", "2024-01-01")]
        assert sort_players(players)[0].player == "B"

    def test_empty(self):
        assert sort_players([]) == []

    def test_stable_full_priority(self):
        players = [
            _p("D", "en", "en",  "720p",  "2024-06-01"),
            _p("C", "jp", "pl",  "720p",  "2024-01-01"),
            _p("A", "pl", "pl",  "1080p", "2024-03-01"),
            _p("B", "pl", "pl",  "720p",  "2024-05-01"),
        ]
        assert [p.player for p in sort_players(players)] == ["A", "B", "C", "D"]

    def test_download_jp_before_pl(self):
        players = [_p("A", "pl", "pl"), _p("B", "jp", "pl")]
        assert sort_players(players, download=True)[0].player == "B"

    def test_download_jp_before_en(self):
        players = [_p("A", "en", "pl"), _p("B", "jp", "pl")]
        assert sort_players(players, download=True)[0].player == "B"

    def test_download_en_before_pl(self):
        players = [_p("A", "pl", "pl"), _p("B", "en", "pl")]
        assert sort_players(players, download=True)[0].player == "B"

    def test_download_full_order(self):
        players = [
            _p("PL",    "pl",  "pl",  "1080p"),
            _p("EN",    "en",  "pl",  "1080p"),
            _p("JP",    "jp",  "pl",  "1080p"),
            _p("OTHER", "ru",  "pl",  "1080p"),
        ]
        assert [p.player for p in sort_players(players, download=True)] == ["JP", "EN", "PL", "OTHER"]

    def test_watch_and_download_modes_differ(self):
        """Same input must produce opposite audio ordering in watch vs download."""
        players = [_p("A", "pl", ""), _p("B", "jp", "")]
        watch = sort_players(players, download=False)
        dl = sort_players(players, download=True)
        assert watch[0].player == "A"
        assert dl[0].player == "B"
