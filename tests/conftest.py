import pytest


@pytest.fixture
def series_episodes_html():
    p = __file__.replace("conftest.py", "") + "fixtures/series_episodes.html"
    with open(p, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def player_show_html():
    return '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=1234567"></iframe></body></html>'


@pytest.fixture
def episode_players_html():
    return """<html><body>
    <td class="ep-buttons">
      <a data-episode="&quot;online_id&quot;:&quot;12345&quot;,&quot;player&quot;:&quot;Sibnet&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;,&quot;max_res&quot;:&quot;720p&quot;">Sibnet</a>
      <a data-episode="{&quot;online_id&quot;:&quot;67890&quot;,&quot;player&quot;:&quot;CDA&quot;,&quot;lang_audio&quot;:&quot;jp&quot;,&quot;lang_subs&quot;:&quot;pl&quot;,&quot;max_res&quot;:&quot;1080p&quot;}">CDA</a>
    </td>
    </body></html>"""
