from alt_ani_cli.shinden.episode import parse_players


def test_parse_players_valid_json(episode_players_html):
    players = parse_players(episode_players_html)
    # Only the second <a> has valid JSON wrapped in { }
    cda = next((p for p in players if p.player == "CDA"), None)
    assert cda is not None
    assert cda.online_id == "67890"
    assert cda.lang_audio == "jp"
    assert cda.lang_subs == "pl"
    assert cda.max_res == "1080p"


def test_parse_players_html_unescape():
    html = """<a data-episode="{&quot;online_id&quot;:&quot;abc&quot;,&quot;player&quot;:&quot;Mp4upload&quot;,&quot;lang_audio&quot;:&quot;pl&quot;,&quot;lang_subs&quot;:&quot;&quot;}">x</a>"""
    players = parse_players(html)
    assert len(players) == 1
    assert players[0].online_id == "abc"
    assert players[0].player == "Mp4upload"
    assert players[0].max_res is None


def test_parse_players_empty_html():
    assert parse_players("<html><body></body></html>") == []


def test_parse_players_skips_missing_fields():
    html = '<a data-episode="{&quot;player&quot;:&quot;CDA&quot;}">x</a>'
    players = parse_players(html)
    assert players == []  # online_id is missing
