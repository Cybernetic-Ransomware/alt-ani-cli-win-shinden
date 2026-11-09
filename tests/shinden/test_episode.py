"""Tests for shinden/episode.py — parse_players (Shinden 2.5 JSON island) and sort_players."""

import json
import re

import pytest

from alt_ani_cli.errors import ParseError
from alt_ani_cli.flow.pin import find_pinned, player_fingerprint
from alt_ani_cli.shinden.episode import parse_players, sort_players
from alt_ani_cli.shinden.models import PlayerEntry

_TRAP_ID = "2045542847"
_JSON_RE = re.compile(r'(<script type="application/json" id="episode-players-data">)(.*?)(</script>)', re.S)


def _p(player, lang_audio, lang_subs, max_res=None, date_added=None):
    return PlayerEntry(
        online_id="x",
        player=player,
        lang_audio=lang_audio,
        lang_subs=lang_subs,
        max_res=max_res,
        date_added=date_added,
    )


def _edit_json(html: str, mutate) -> str:
    """Apply mutate(data) to the embedded player JSON of a real episode page."""
    m = _JSON_RE.search(html)
    data = json.loads(m.group(2))
    mutate(data)
    return html[: m.start(2)] + json.dumps(data, ensure_ascii=False) + html[m.end(2) :]


def _replace_json(html: str, raw: str) -> str:
    m = _JSON_RE.search(html)
    return html[: m.start(2)] + raw + html[m.end(2) :]


def _events(events, name):
    return [fields for event, fields in events if event == name]


@pytest.mark.unit
class TestParsePlayersFixture:
    def test_all_real_players_parsed(self, shinden_html, diag_events):
        players = parse_players(shinden_html("episode_players.html"))
        assert len(players) == 9
        assert diag_events == []

    def test_honeypot_never_returned(self, shinden_html):
        html = shinden_html("episode_players.html")
        assert _TRAP_ID in html
        assert _TRAP_ID not in {p.online_id for p in parse_players(html)}

    def test_field_mapping(self, shinden_html):
        first = parse_players(shinden_html("episode_players.html"))[0]
        assert first == PlayerEntry(
            online_id="1825161",
            player="Gdrive",
            lang_audio="jp",
            lang_subs="pl",
            max_res="1080p",
            date_added=None,
            subs_author="Example Subs",
            source="https://subs.example/",
        )

    def test_player_without_group_has_no_author_or_source(self, shinden_html):
        dood = next(p for p in parse_players(shinden_html("episode_players.html")) if p.online_id == "1598004")
        assert (dood.subs_author, dood.source, dood.max_res) == (None, None, "720p")

    def test_json_order_preserved(self, shinden_html):
        ids = [p.online_id for p in parse_players(shinden_html("episode_players.html"))]
        assert ids[:3] == ["1825161", "1825160", "1879668"]

    def test_sort_players_on_parsed_output(self, shinden_html):
        ranked = sort_players(parse_players(shinden_html("episode_players.html")))
        assert [p.max_res for p in ranked[:6]] == ["1080p"] * 6
        assert ranked[-1].lang_subs == "en"

    def test_empty_players_list_is_valid(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d.update(players=[]))
        html = re.sub(r'<li class="ep-pl-row" data-online-id="\d+">', '<li class="ep-pl-gone">', html)
        assert parse_players(html) == []
        assert diag_events == []


@pytest.mark.unit
class TestParsePlayersDrift:
    def test_missing_script_raises(self, shinden_html):
        html = shinden_html("episode_players.html").replace('id="episode-players-data"', 'id="renamed"')
        with pytest.raises(ParseError, match="episode-players-data"):
            parse_players(html)

    def test_page_without_any_player_markup_raises(self):
        with pytest.raises(ParseError):
            parse_players("<html><body><p>no players here</p></body></html>")

    def test_invalid_json_raises(self, shinden_html):
        with pytest.raises(ParseError, match="not valid JSON"):
            parse_players(_replace_json(shinden_html("episode_players.html"), "{not json"))

    @pytest.mark.parametrize("data", [[], {"players": {}}, {"no_players": []}])
    def test_wrong_top_level_shape_raises(self, shinden_html, data):
        with pytest.raises(ParseError, match="no players list"):
            parse_players(_replace_json(shinden_html("episode_players.html"), json.dumps(data)))

    def test_honeypot_of_wrong_type_raises(self, shinden_html):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d.update(pulapka="trap"))
        with pytest.raises(ParseError, match="honeypot"):
            parse_players(html)

    def test_missing_honeypot_is_fine(self, shinden_html):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d.pop("pulapka"))
        assert len(parse_players(html)) == 9

    def test_challenge_page_raises(self):
        with pytest.raises(ParseError, match="Cloudflare challenge"):
            parse_players("<html><head><title>Just a moment...</title></head></html>")


@pytest.mark.unit
class TestParsePlayersPartial:
    @pytest.mark.parametrize(
        "patch, reason",
        [
            ({"online_id": None}, "bad_online_id"),
            ({"online_id": True}, "bad_online_id"),
            ({"online_id": -5}, "bad_online_id"),
            ({"player": ""}, "bad_player"),
            ({"player": 7}, "bad_player"),
            ({"audio": 1}, "bad_lang"),
        ],
    )
    def test_single_invalid_entry_is_skipped_and_logged(self, shinden_html, diag_events, patch, reason):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"][0].update(patch))
        assert len(parse_players(html)) == 8
        (partial,) = _events(diag_events, "shinden_parse_partial")
        assert partial["invalid"] == f"{reason}:1"
        assert (partial["found"], partial["valid"]) == (9, 8)

    def test_non_dict_entry_is_invalid(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"].append("garbage"))
        assert len(parse_players(html)) == 9
        assert _events(diag_events, "shinden_parse_partial")[0]["invalid"] == "bad_online_id:1"

    def test_all_entries_invalid_raises(self, shinden_html):
        def _break(d):
            for p in d["players"]:
                p["player"] = ""

        with pytest.raises(ParseError, match="all 9 expected records are invalid"):
            parse_players(_edit_json(shinden_html("episode_players.html"), _break))

    def test_invalid_plus_honeypot_only_still_raises(self, shinden_html):
        def _only_trap_and_broken(d):
            d["players"] = [{"online_id": d["pulapka"], "player": "Trap"}, {"online_id": 5, "player": ""}]

        with pytest.raises(ParseError):
            parse_players(_edit_json(shinden_html("episode_players.html"), _only_trap_and_broken))

    def test_honeypot_only_returns_empty_without_error(self, shinden_html, diag_events):
        def _only_trap(d):
            d["players"] = [{"online_id": d["pulapka"], "player": "Trap"}]

        assert parse_players(_edit_json(shinden_html("episode_players.html"), _only_trap)) == []
        assert _events(diag_events, "shinden_parse_partial") == []

    def test_duplicate_entry_is_excluded(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"].append(dict(d["players"][0])))
        assert len(parse_players(html)) == 9
        assert _events(diag_events, "shinden_parse_partial") == []

    def test_string_online_id_accepted(self, shinden_html):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"][0].update(online_id="1825161"))
        assert parse_players(html)[0].online_id == "1825161"

    def test_malformed_optional_fields_are_dropped(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"][0].update(res="hd", group=[1]))
        first = parse_players(html)[0]
        assert (first.max_res, first.subs_author, first.source) == (None, None, None)
        assert _events(diag_events, "shinden_parse_partial") == []


@pytest.mark.unit
class TestPlayersDomConsistency:
    def test_healthy_page_has_no_mismatch(self, shinden_html, diag_events):
        parse_players(shinden_html("episode_players.html"))
        assert _events(diag_events, "shinden_players_mismatch") == []

    def test_visible_row_missing_from_json(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"].pop(0))
        assert len(parse_players(html)) == 8
        assert _events(diag_events, "shinden_players_mismatch") == [
            {"dom_only": 1, "json_only": 0, "trap_visible": False, "dom_duplicates": 0}
        ]

    def test_json_player_missing_from_dom(self, shinden_html, diag_events):
        html = shinden_html("episode_players.html").replace('data-online-id="1825161"', 'data-gone="1825161"')
        parse_players(html)
        assert _events(diag_events, "shinden_players_mismatch")[0]["json_only"] == 1

    def test_visible_honeypot_row_is_reported(self, shinden_html, diag_events):
        html = shinden_html("episode_players.html").replace(
            f'data-online-id="{_TRAP_ID}" hidden', f'data-online-id="{_TRAP_ID}"'
        )
        players = parse_players(html)
        assert _TRAP_ID not in {p.online_id for p in players}
        assert _events(diag_events, "shinden_players_mismatch")[0]["trap_visible"] is True

    def test_invalid_json_entry_shown_in_dom_is_not_dom_only(self, shinden_html, diag_events):
        html = _edit_json(shinden_html("episode_players.html"), lambda d: d["players"][0].update(player=""))
        parse_players(html)
        assert _events(diag_events, "shinden_players_mismatch") == []

    def test_duplicate_dom_rows_are_counted(self, shinden_html, diag_events):
        row = '<li class="ep-pl-row" data-online-id="1825161"></li>'
        html = shinden_html("episode_players.html").replace('<ul class="ep-pl-list">', '<ul class="ep-pl-list">' + row)
        parse_players(html)
        assert _events(diag_events, "shinden_players_mismatch")[0]["dom_duplicates"] == 1


@pytest.mark.unit
class TestPinCompatibility:
    def test_pin_from_one_episode_matches_same_source_in_next(self, shinden_html):
        """Pins hold group name/url + host/lang/res, none of which change between episodes."""
        ep1 = parse_players(shinden_html("episode_players.html"))

        def _next_episode(d):
            for p in d["players"]:
                p["online_id"] += 1000

        ep2 = parse_players(_edit_json(shinden_html("episode_players.html"), _next_episode))
        match = find_pinned(ep2, player_fingerprint(ep1[2]))
        assert match is not None
        assert (match.player, match.subs_author, match.source) == ("Vidara", "Example Subs", "https://subs.example/")
        assert match.online_id != ep1[2].online_id

    def test_different_group_does_not_match_pin(self, shinden_html):
        pin = player_fingerprint(parse_players(shinden_html("episode_players.html"))[0])

        def _regroup(d):
            for p in d["players"]:
                if p.get("group"):
                    p["group"] = {"name": "Other Subs", "url": "https://other.example/"}

        other = parse_players(_edit_json(shinden_html("episode_players.html"), _regroup))
        assert find_pinned(other, pin) is None


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
