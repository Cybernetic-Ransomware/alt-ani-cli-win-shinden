"""Text contract of rendered UI labels.

These tests pin the label *format* (stable technical tokens: id, date, counts,
watched marker distinguishability, template placeholders) — not menu behaviour,
which lives in tests/ui/test_menus.py.
"""

from unittest.mock import patch

import pytest

from alt_ani_cli.content import CONTENT
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesMetadata
from alt_ani_cli.ui.menus import _player_label, _player_origin, _series_label, select_episodes, select_start_mode

_HIT = SeriesHit(id="234", slug="ikkitousen", title="Ikkitousen", url="https://shinden.pl/series/234-ikkitousen", series_type="TV")
_META = SeriesMetadata(air_date="30.07.2003", air_date_sort=(2003, 7, 30), description="", tags=(), related=())


@pytest.mark.unit
class TestSeriesLabel:
    def test_contains_id_without_metadata(self):
        assert _HIT.id in _series_label(_HIT, None)

    def test_contains_id_and_date_with_metadata(self):
        label = _series_label(_HIT, _META)
        assert _HIT.id in label
        assert _META.air_date in label


@pytest.mark.unit
class TestEpisodeLabels:
    def test_watched_and_unwatched_templates_differ(self):
        _ep = CONTENT["menu"]["episodes"]
        watched = _ep["label_watched"].format(number=1.0, title="Ep A")
        unwatched = _ep["label_unwatched"].format(number=1.0, title="Ep A")
        assert watched != unwatched

    def test_select_episodes_renders_watched_template(self, monkeypatch):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        episodes = [
            EpisodeRow(number=1, title="Ep A", url="http://x/1"),
            EpisodeRow(number=2, title="Ep B", url="http://x/2"),
        ]
        printed: list[str] = []
        with (
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
            patch("builtins.input", return_value="2"),
        ):
            select_episodes(episodes, watched_numbers={1.0})
        _ep = CONTENT["menu"]["episodes"]
        assert any(_ep["label_watched"].format(number=1, title="Ep A") in line for line in printed)
        assert any(_ep["label_unwatched"].format(number=2, title="Ep B") in line for line in printed)


def _player(**overrides):
    defaults = dict(online_id="p1", player="CDA", lang_audio="jp", lang_subs="pl", max_res="1080p")
    defaults.update(overrides)
    return PlayerEntry(**defaults)


@pytest.mark.unit
class TestPlayerOriginLabels:
    def test_author_similar_to_host_shows_author_only(self):
        origin = _player_origin(_player(subs_author="Mioro-Subs", source="https://miorosubs.com/"))
        assert "Mioro-Subs" in origin
        assert "miorosubs.com" not in origin

    def test_author_and_host_divergent_shows_both(self):
        origin = _player_origin(
            _player(subs_author="Aniplex of America", source="http://feeds.feedburner.com/crunchyroll/rss/anime?format=xml")
        )
        assert "Aniplex of America" in origin
        assert "feedburner.com" in origin

    def test_long_source_url_never_shown_verbatim(self):
        url = "http://feeds.feedburner.com/crunchyroll/rss/anime?format=xml"
        origin = _player_origin(_player(source=url))
        assert url not in origin
        assert "feedburner.com" in origin

    def test_no_data_yields_clean_label(self):
        label = _player_label(_player(), failed=set(), sources=None)
        assert label == label.rstrip()
        assert "—" not in label
        assert "None" not in label

    def test_resolved_host_appended(self):
        origin = _player_origin(_player(), resolved_host="kerapoxy.cc")
        assert "kerapoxy.cc" in origin

    def test_failed_and_ok_labels_differ(self):
        p = _player()
        ok = _player_label(p, failed=set(), sources=None)
        bad = _player_label(p, failed={p.online_id}, sources=None)
        assert ok != bad


@pytest.mark.unit
class TestStartModeLabels:
    def test_resume_count_in_label(self, monkeypatch, capsys):
        monkeypatch.setattr("alt_ani_cli.ui.menus._USE_INQUIRER", False)
        with patch("builtins.input", return_value="2"):
            select_start_mode(has_history=True, history_count=5)
        assert "5" in capsys.readouterr().out


@pytest.mark.unit
class TestTemplatePlaceholders:
    """Templates rendered with .format() must keep their placeholders — a broken
    key in the YAML would otherwise only blow up at runtime."""

    @pytest.mark.parametrize(
        "path, placeholders",
        [
            (("menu", "series", "label_with_date"), ("{title}", "{id}", "{date}")),
            (("menu", "series", "label_without_date"), ("{title}", "{id}")),
            (("menu", "episodes", "label_watched"), ("{number", "{title}")),
            (("menu", "episodes", "label_unwatched"), ("{number", "{title}")),
            (("menu", "start_mode", "options", "resume_with_count"), ("{count}",)),
            (("menu", "player", "label_ok"), ("{player}", "{res}", "{audio}", "{subs}", "{origin}")),
            (("menu", "player", "label_failed"), ("{player}", "{res}", "{audio}", "{subs}", "{origin}")),
            (("menu", "player", "origin_author"), ("{author}",)),
            (("menu", "player", "origin_author_host"), ("{author}", "{host}")),
            (("menu", "player", "origin_host"), ("{host}",)),
            (("menu", "player", "resolved_host"), ("{host}",)),
            (("menu", "player", "source_header"), ("{player}",)),
            (("progress", "extractor_fallback"), ("{host}", "{exc}")),
            (("progress", "jwplayer_fallback"), ("{host}", "{exc}")),
            (("progress", "prefetch_sources"), ("{count}",)),
        ],
    )
    def test_template_has_required_placeholders(self, path, placeholders):
        tmpl = CONTENT
        for key in path:
            tmpl = tmpl[key]
        for ph in placeholders:
            assert ph in tmpl, f"{'.'.join(path)} missing {ph}"
