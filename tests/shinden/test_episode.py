"""Tests for shinden/episode.py — read_players/parse_players (Shinden 2.5 JSON island) and sort_players."""

import json
import re

import pytest
from alt_ani_core.dom import parse as parse_html
from alt_ani_core.errors import ParseError
from alt_ani_core.models import PlayerEntry
from alt_ani_core.rules.pin import find_pinned, player_fingerprint
from alt_ani_core.rules.ranking import sort_players
from alt_ani_core.shinden.episode import _online_id, parse_players, read_players

from .conftest import TEST_TOKEN

_TRAP_ID = "2045542847"
_HIDDEN_ROW = f'data-online-id="{_TRAP_ID}" hidden="" aria-hidden="true"'
_VISIBLE_ROW_RE = re.compile(r'<li class="ep-pl-row" data-online-id="\d+">')
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


def _with_row(html: str, row: str) -> str:
    return html.replace('<ul class="ep-pl-list">', '<ul class="ep-pl-list">' + row, 1)


def _outside_list(html: str, markup: str) -> str:
    return html.replace("<body>", "<body>" + markup, 1)


@pytest.fixture
def fixture_html(shinden_html) -> str:
    return shinden_html("episode_players.html")


def _events(events, name):
    return [fields for event, fields in events if event == name]


@pytest.mark.unit
class TestParsePlayersFixture:
    def test_all_real_players_parsed(self, fixture_html, diag_events):
        players = parse_players(fixture_html)
        assert len(players) == 9
        assert diag_events == []

    def test_honeypot_never_returned(self, fixture_html):
        html = fixture_html
        assert _TRAP_ID in html
        assert _TRAP_ID not in {p.online_id for p in parse_players(html)}

    def test_field_mapping(self, fixture_html):
        first = parse_players(fixture_html)[0]
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

    def test_player_without_group_has_no_author_or_source(self, fixture_html):
        dood = next(p for p in parse_players(fixture_html) if p.online_id == "1598004")
        assert (dood.subs_author, dood.source, dood.max_res) == (None, None, "720p")

    def test_json_order_preserved(self, fixture_html):
        ids = [p.online_id for p in parse_players(fixture_html)]
        assert ids[:3] == ["1825161", "1825160", "1879668"]

    def test_sort_players_on_parsed_output(self, fixture_html):
        ranked = sort_players(parse_players(fixture_html))
        assert [p.max_res for p in ranked[:6]] == ["1080p"] * 6
        assert ranked[-1].lang_subs == "en"

    def test_empty_players_list_is_valid(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d.update(players=[]))
        html = _VISIBLE_ROW_RE.sub('<li class="ep-pl-gone">', html)
        assert parse_players(html) == []
        assert diag_events == []


@pytest.mark.unit
class TestParsePlayersDrift:
    def test_missing_script_raises(self, fixture_html):
        html = fixture_html.replace('id="episode-players-data"', 'id="renamed"')
        with pytest.raises(ParseError, match="episode-players-data"):
            parse_players(html)

    def test_page_without_any_player_markup_raises(self):
        with pytest.raises(ParseError):
            parse_players("<html><body><p>no players here</p></body></html>")

    @pytest.mark.parametrize("raw", ["{not json", '{"players": [], "pulapka": ' + "9" * 5000 + "}"], ids=["syntax", "huge_int"])
    def test_invalid_json_raises(self, fixture_html, raw):
        with pytest.raises(ParseError, match="not valid JSON"):
            parse_players(_replace_json(fixture_html, raw))

    @pytest.mark.parametrize("data", [[], {"players": {}}, {"no_players": []}])
    def test_wrong_top_level_shape_raises(self, fixture_html, data):
        with pytest.raises(ParseError, match="no players list"):
            parse_players(_replace_json(fixture_html, json.dumps(data)))

    def test_honeypot_of_wrong_type_raises(self, fixture_html):
        html = _edit_json(fixture_html, lambda d: d.update(pulapka="trap"))
        with pytest.raises(ParseError, match="honeypot"):
            parse_players(html)

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
            ({"online_id": 0}, "bad_online_id"),
            ({"online_id": 1.5}, "bad_online_id"),
            ({"online_id": 10**30}, "bad_online_id"),
            ({"online_id": "\u00b2"}, "bad_online_id"),
            ({"online_id": "\u0661\u0662\u0663"}, "bad_online_id"),
            pytest.param({"online_id": "9" * 5000}, "bad_online_id", id="huge_digit_string"),
            ({"online_id": "000"}, "bad_online_id"),
            ({"player": ""}, "bad_player"),
            ({"player": 7}, "bad_player"),
            ({"audio": 1}, "bad_lang"),
        ],
    )
    def test_single_invalid_entry_is_skipped_and_logged(self, fixture_html, diag_events, patch, reason):
        html = _edit_json(fixture_html, lambda d: d["players"][0].update(patch))
        assert len(parse_players(html)) == 8
        (partial,) = _events(diag_events, "shinden_parse_partial")
        assert partial["invalid"] == f"{reason}:1"
        assert (partial["found"], partial["valid"]) == (9, 8)

    def test_non_dict_entry_is_invalid(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d["players"].append("garbage"))
        assert len(parse_players(html)) == 9
        assert _events(diag_events, "shinden_parse_partial")[0]["invalid"] == "bad_online_id:1"

    def test_all_entries_invalid_raises(self, fixture_html):
        def _break(d):
            for p in d["players"]:
                p["player"] = ""

        with pytest.raises(ParseError, match="all 9 expected records are invalid"):
            parse_players(_edit_json(fixture_html, _break))

    def test_invalid_plus_honeypot_only_still_raises(self, fixture_html):
        def _only_trap_and_broken(d):
            d["players"] = [{"online_id": d["pulapka"], "player": "Trap"}, {"online_id": 5, "player": ""}]

        with pytest.raises(ParseError):
            parse_players(_edit_json(fixture_html, _only_trap_and_broken))

    def test_honeypot_only_without_visible_rows_returns_empty(self, fixture_html, diag_events):
        def _only_trap(d):
            d["players"] = [{"online_id": d["pulapka"], "player": "Trap"}]

        html = _VISIBLE_ROW_RE.sub('<li class="ep-pl-gone">', fixture_html)
        assert parse_players(_edit_json(html, _only_trap)) == []
        assert _events(diag_events, "shinden_parse_partial") == []

    def test_duplicate_entry_is_excluded(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d["players"].append(dict(d["players"][0])))
        assert len(parse_players(html)) == 9
        assert _events(diag_events, "shinden_parse_partial") == []

    @pytest.mark.parametrize("value", ["1825161", "0001825161", " 1825161 "])
    def test_string_online_id_accepted(self, fixture_html, diag_events, value):
        html = _edit_json(fixture_html, lambda d: d["players"][0].update(online_id=value))
        assert parse_players(html)[0].online_id == "1825161"
        assert diag_events == []

    def test_malformed_optional_fields_are_dropped(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d["players"][0].update(res="hd", group=[1]))
        first = parse_players(html)[0]
        assert (first.max_res, first.subs_author, first.source) == (None, None, None)
        assert _events(diag_events, "shinden_parse_partial") == []


@pytest.mark.unit
class TestPlayersDomConsistency:
    def test_healthy_page_has_no_mismatch(self, fixture_html, diag_events):
        parse_players(fixture_html)
        assert _events(diag_events, "shinden_players_mismatch") == []

    def test_visible_row_missing_from_json(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d["players"].pop(0))
        assert len(parse_players(html)) == 8
        assert _events(diag_events, "shinden_players_mismatch") == [
            {"dom_only": 1, "json_only": 0, "trap_visible": False, "dom_duplicates": 0}
        ]

    def test_json_player_missing_from_dom(self, fixture_html, diag_events):
        html = fixture_html.replace('data-online-id="1825161"', 'data-gone="1825161"')
        parse_players(html)
        assert _events(diag_events, "shinden_players_mismatch")[0]["json_only"] == 1

    def test_visible_honeypot_row_is_reported(self, fixture_html, diag_events):
        html = fixture_html.replace(_HIDDEN_ROW, f'data-online-id="{_TRAP_ID}"')
        players = parse_players(html)
        assert _TRAP_ID not in {p.online_id for p in players}
        assert _events(diag_events, "shinden_players_mismatch")[0]["trap_visible"] is True

    def test_invalid_json_entry_shown_in_dom_is_not_dom_only(self, fixture_html, diag_events):
        html = _edit_json(fixture_html, lambda d: d["players"][0].update(player=""))
        parse_players(html)
        assert _events(diag_events, "shinden_players_mismatch") == []

    @pytest.mark.parametrize(
        "markup",
        [
            '<div data-online-id="777" hidden=""></div>',
            '<span data-online-id="abc" aria-hidden="true"></span>',
            '<div data-online-id="1825161"></div><li class="ep-pl-row" data-online-id="555"></li>',
        ],
        ids=["hidden_other_id", "hidden_invalid_id", "visible_rows"],
    )
    def test_markup_outside_the_player_list_is_ignored(self, fixture_html, diag_events, markup):
        assert len(parse_players(_outside_list(fixture_html, markup))) == 9
        assert diag_events == []

    def test_row_in_nested_list_is_counted_once(self, fixture_html, diag_events):
        nested = '<li><ul class="ep-pl-list"><li class="ep-pl-row" data-online-id="1825161"></li></ul></li>'
        parse_players(_with_row(fixture_html, nested))
        assert _events(diag_events, "shinden_players_mismatch")[0]["dom_duplicates"] == 1

    def test_duplicate_dom_rows_are_counted(self, fixture_html, diag_events):
        parse_players(_with_row(fixture_html, '<li class="ep-pl-row" data-online-id="1825161"></li>'))
        assert _events(diag_events, "shinden_players_mismatch")[0]["dom_duplicates"] == 1


@pytest.mark.unit
class TestReadPlayers:
    def test_page_contract_is_read_once(self, fixture_html):
        page = read_players(parse_html(fixture_html))
        assert len(page.players) == 9
        assert page.player_ids == frozenset(p.online_id for p in page.players)
        assert (page.trap_id, page.xhr_endpoint) == (_TRAP_ID, "//shinden.pl/xhr")
        assert parse_players(fixture_html) == list(page.players)

    def test_auth_token_never_reaches_the_result(self, episode_page):
        assert TEST_TOKEN not in repr(read_players(parse_html(episode_page())))


@pytest.mark.unit
class TestHoneypotPolicy:
    @pytest.mark.parametrize("hidden_row", [True, False])
    def test_missing_declaration_raises(self, fixture_html, hidden_row):
        html = fixture_html if hidden_row else fixture_html.replace(_HIDDEN_ROW, 'class="gone"')
        with pytest.raises(ParseError, match="honeypot declaration is missing"):
            parse_players(_edit_json(html, lambda d: d.pop("pulapka")))

    @pytest.mark.parametrize("value", ["trap", 0, -1, True, 1.5, [], {}, ""])
    def test_invalid_value_raises(self, fixture_html, value):
        with pytest.raises(ParseError, match="honeypot id has an unexpected type"):
            parse_players(_edit_json(fixture_html, lambda d: d.update(pulapka=value)))

    def test_null_without_hidden_row_means_no_honeypot(self, fixture_html):
        html = _edit_json(fixture_html.replace(_HIDDEN_ROW, 'class="gone"'), lambda d: d.update(pulapka=None))
        page = read_players(parse_html(html))
        assert (page.trap_id, len(page.players)) == (None, 9)

    @pytest.mark.parametrize("value", [None, 1234], ids=["null", "other_id"])
    def test_declaration_disagreeing_with_hidden_row_raises(self, fixture_html, value):
        with pytest.raises(ParseError, match="hidden player row"):
            parse_players(_edit_json(fixture_html, lambda d: d.update(pulapka=value)))

    @pytest.mark.parametrize(
        "row",
        [
            '<li class="ep-pl-row" hidden=""></li>',
            '<li class="ep-pl-row" data-online-id="abc" hidden=""></li>',
            '<li class="ep-pl-row" data-online-id="" aria-hidden="true"></li>',
            '<li class="ep-pl-row" data-online-id="\u00b2" hidden=""></li>',
            '<li class="ep-pl-row" data-online-id="\u0662\u0660\u0664" aria-hidden="true"></li>',
            pytest.param(f'<li class="ep-pl-row" data-online-id="{"9" * 5000}" hidden=""></li>', id="huge_id"),
            '<div data-online-id="777" hidden=""></div>',
        ],
    )
    def test_extra_hidden_row_without_the_honeypot_id_raises(self, fixture_html, row):
        with pytest.raises(ParseError, match="hidden player row"):
            parse_players(_with_row(fixture_html, row))

    @pytest.mark.parametrize("row_id", [_TRAP_ID, f"0{_TRAP_ID}"])
    def test_extra_hidden_honeypot_row_never_becomes_a_player(self, fixture_html, row_id):
        players = parse_players(_with_row(fixture_html, f'<li class="ep-pl-row" data-online-id="{row_id}" hidden=""></li>'))
        assert len(players) == 9
        assert _TRAP_ID not in {p.online_id for p in players}

    def test_honeypot_inside_players_is_excluded(self, fixture_html):
        def _add_trap(d):
            d["players"].insert(0, dict(d["players"][0], online_id=d["pulapka"]))

        page = read_players(parse_html(_edit_json(fixture_html, _add_trap)))
        assert _TRAP_ID not in page.player_ids | {p.online_id for p in page.players}


@pytest.mark.unit
class TestDomCriticalChecks:
    @pytest.mark.parametrize(
        "players",
        [[], [{"online_id": int(_TRAP_ID), "player": "Trap"}]],
        ids=["empty", "honeypot_only"],
    )
    def test_no_valid_player_with_visible_rows_raises(self, fixture_html, players):
        with pytest.raises(ParseError, match="9 player rows are visible"):
            parse_players(_edit_json(fixture_html, lambda d: d.update(players=players)))

    def test_visible_row_without_id_counts_against_empty_json(self, fixture_html):
        html = _with_row(_VISIBLE_ROW_RE.sub('<li class="ep-pl-gone">', fixture_html), '<li class="ep-pl-row"></li>')
        with pytest.raises(ParseError, match="1 player rows are visible"):
            parse_players(_edit_json(html, lambda d: d.update(players=[])))

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            (_HIDDEN_ROW, f'data-online-id="{_TRAP_ID}" aria-hidden="true"'),
            ('data-online-id="1825161"', 'data-online-id="01825161"'),
        ],
        ids=["aria_hidden_only", "zero_padded_id"],
    )
    def test_equivalent_dom_markup_is_no_mismatch(self, fixture_html, diag_events, old, new):
        parse_players(fixture_html.replace(old, new))
        assert _events(diag_events, "shinden_players_mismatch") == []

    @pytest.mark.parametrize(
        "row",
        [
            '<li class="ep-pl-row"></li>',
            '<li class="ep-pl-row" data-online-id="abc"></li>',
            '<li class="ep-pl-row" data-online-id="\u00b2"></li>',
            '<li class="ep-pl-row" data-online-id="\u0661\u0662"></li>',
            f'<li class="ep-pl-row" data-online-id="{"9" * 5000}"></li>',
            '<div data-online-id="0"></div>',
        ],
        ids=["missing", "letters", "superscript", "non_ascii_digits", "too_long", "zero"],
    )
    def test_visible_row_with_invalid_id_is_only_reported(self, fixture_html, diag_events, row):
        assert len(parse_players(_with_row(fixture_html, row))) == 9
        assert _events(diag_events, "shinden_players_mismatch")[0]["dom_only"] == 1


def _rename_lang_and_res(d):
    for p in d["players"]:
        p["lang_audio"], p["max_res"] = p.pop("audio"), p.pop("res")


@pytest.mark.unit
class TestPlayersDegraded:
    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (lambda d: None, []),
            (_rename_lang_and_res, [{"missing": "audio,res", "affected": 9}]),
            (lambda d: d["players"][0].pop("sub"), []),
            (lambda d: [p.pop("sub") for p in d["players"]], [{"missing": "sub", "affected": 9}]),
            (lambda d: [p.pop("group", None) for p in d["players"]], []),
            (lambda d: d["players"][0].update(audio="", sub=None, res=None, group=None), []),
            (lambda d: d["players"].append({"online_id": d["pulapka"], "player": "Trap"}), []),
            (lambda d: d["players"].append({"online_id": 5, "player": ""}), []),
        ],
        ids=[
            "complete",
            "renamed_keys",
            "one_record_missing_key",
            "key_missing_everywhere",
            "group_is_optional",
            "empty_values",
            "excluded_record",
            "rejected_record",
        ],
    )
    def test_missing_contract_keys_are_reported_once(self, fixture_html, diag_events, mutate, expected):
        parse_players(_edit_json(fixture_html, mutate))
        assert _events(diag_events, "shinden_players_degraded") == expected


@pytest.mark.unit
class TestOnlineId:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1825161, "1825161"),
            ("1825161", "1825161"),
            ("0001825161", "1825161"),
            (" 42 ", "42"),
            ("9" * 19, "9" * 19),
            (10**19 - 1, "9" * 19),
        ],
    )
    def test_valid(self, value, expected):
        assert _online_id(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            0,
            -1,
            1.5,
            10**19,
            pytest.param(10**5000, id="huge_int"),
            "",
            "0",
            "000",
            "-5",
            "+5",
            "1_000",
            "1e3",
            "\u00b2",
            "\u0661\u0662\u0663",
            "\uff11\uff12",
            "9" * 20,
            pytest.param("9" * 5000, id="huge_digit_string"),
            None,
            [],
            {},
        ],
    )
    def test_invalid(self, value):
        assert _online_id(value) is None


@pytest.mark.unit
class TestPinCompatibility:
    def test_pin_from_one_episode_matches_same_source_in_next(self, fixture_html):
        """Pins hold group name/url + host/lang/res, none of which change between episodes."""
        ep1 = parse_players(fixture_html)

        def _next_episode(d):
            for p in d["players"]:
                p["online_id"] += 1000

        ep2 = parse_players(_edit_json(fixture_html, _next_episode))
        match = find_pinned(ep2, player_fingerprint(ep1[2]))
        assert match is not None
        assert (match.player, match.subs_author, match.source) == ("Vidara", "Example Subs", "https://subs.example/")
        assert match.online_id != ep1[2].online_id

    def test_different_group_does_not_match_pin(self, fixture_html):
        pin = player_fingerprint(parse_players(fixture_html)[0])

        def _regroup(d):
            for p in d["players"]:
                if p.get("group"):
                    p["group"] = {"name": "Other Subs", "url": "https://other.example/"}

        other = parse_players(_edit_json(fixture_html, _regroup))
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
            _p("D", "en", "en", "720p", "2024-06-01"),
            _p("C", "jp", "pl", "720p", "2024-01-01"),
            _p("A", "pl", "pl", "1080p", "2024-03-01"),
            _p("B", "pl", "pl", "720p", "2024-05-01"),
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
            _p("PL", "pl", "pl", "1080p"),
            _p("EN", "en", "pl", "1080p"),
            _p("JP", "jp", "pl", "1080p"),
            _p("OTHER", "ru", "pl", "1080p"),
        ]
        assert [p.player for p in sort_players(players, download=True)] == ["JP", "EN", "PL", "OTHER"]

    def test_watch_and_download_modes_differ(self):
        """Same input must produce opposite audio ordering in watch vs download."""
        players = [_p("A", "pl", ""), _p("B", "jp", "")]
        watch = sort_players(players, download=False)
        dl = sort_players(players, download=True)
        assert watch[0].player == "A"
        assert dl[0].player == "B"
