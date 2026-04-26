from alt_ani_cli.shinden.episode import sort_players, parse_players
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


def test_sort_audio_pl_first():
    players = [
        _p("Sibnet", "jp", "pl"),
        _p("CDA", "pl", "pl"),
    ]
    result = sort_players(players)
    assert result[0].player == "CDA"


def test_sort_audio_then_subs():
    players = [
        _p("A", "jp", "pl"),
        _p("B", "jp", "en"),
        _p("C", "jp", ""),
    ]
    result = sort_players(players)
    assert [p.player for p in result] == ["A", "B", "C"]


def test_sort_audio_then_res():
    players = [
        _p("A", "jp", "pl", "720p"),
        _p("B", "jp", "pl", "1080p"),
    ]
    result = sort_players(players)
    assert result[0].player == "B"


def test_sort_date_desc_tiebreaker():
    players = [
        _p("A", "jp", "pl", "720p", "2024-01-01"),
        _p("B", "jp", "pl", "720p", "2024-06-15"),
    ]
    result = sort_players(players)
    assert result[0].player == "B"


def test_sort_none_date_last():
    players = [
        _p("A", "jp", "pl", "720p", None),
        _p("B", "jp", "pl", "720p", "2024-01-01"),
    ]
    result = sort_players(players)
    assert result[0].player == "B"


def test_sort_empty():
    assert sort_players([]) == []


def test_sort_stable_full_priority():
    players = [
        _p("D", "en", "en",  "720p",  "2024-06-01"),
        _p("C", "jp", "pl",  "720p",  "2024-01-01"),
        _p("A", "pl", "pl",  "1080p", "2024-03-01"),
        _p("B", "pl", "pl",  "720p",  "2024-05-01"),
    ]
    result = sort_players(players)
    assert [p.player for p in result] == ["A", "B", "C", "D"]


def test_date_parsed_from_table_row():
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


# ---------------------------------------------------------------------------
# Download mode: jp > en > pl > others
# ---------------------------------------------------------------------------

def test_download_jp_before_pl():
    players = [
        _p("A", "pl", "pl"),
        _p("B", "jp", "pl"),
    ]
    result = sort_players(players, download=True)
    assert result[0].player == "B"


def test_download_jp_before_en():
    players = [
        _p("A", "en", "pl"),
        _p("B", "jp", "pl"),
    ]
    result = sort_players(players, download=True)
    assert result[0].player == "B"


def test_download_en_before_pl():
    players = [
        _p("A", "pl", "pl"),
        _p("B", "en", "pl"),
    ]
    result = sort_players(players, download=True)
    assert result[0].player == "B"


def test_download_full_order():
    players = [
        _p("PL",    "pl",  "pl",  "1080p"),
        _p("EN",    "en",  "pl",  "1080p"),
        _p("JP",    "jp",  "pl",  "1080p"),
        _p("OTHER", "ru",  "pl",  "1080p"),
    ]
    result = sort_players(players, download=True)
    assert [p.player for p in result] == ["JP", "EN", "PL", "OTHER"]


def test_download_watch_modes_differ():
    """Same input must produce opposite audio ordering in watch vs download."""
    players = [
        _p("A", "pl", ""),
        _p("B", "jp", ""),
    ]
    watch = sort_players(players, download=False)
    dl    = sort_players(players, download=True)
    assert watch[0].player == "A"   # PL dub first for watching
    assert dl[0].player    == "B"   # JP original first for archiving


def test_date_none_when_no_column():
    html = """<html><body>
    <table><tbody>
      <tr>
        <td><a data-episode="{&quot;online_id&quot;:&quot;2&quot;,&quot;player&quot;:&quot;Sibnet&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;}">Sibnet</a></td>
      </tr>
    </tbody></table>
    </body></html>"""
    players = parse_players(html)
    assert players[0].date_added is None
