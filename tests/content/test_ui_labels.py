"""Text contract of rendered UI labels.

These tests pin the label *format* (stable technical tokens: id, date, counts,
watched marker distinguishability, template placeholders) — not menu behaviour,
which lives in tests/ui/test_menus.py.
"""

from unittest.mock import patch

import pytest

from alt_ani_cli.content import CONTENT
from alt_ani_cli.shinden.models import EpisodeRow, SeriesHit, SeriesMetadata
from alt_ani_cli.ui.menus import _series_label, select_episodes, select_start_mode

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
            (("progress", "extractor_fallback"), ("{host}", "{exc}")),
            (("progress", "jwplayer_fallback"), ("{host}", "{exc}")),
        ],
    )
    def test_template_has_required_placeholders(self, path, placeholders):
        tmpl = CONTENT
        for key in path:
            tmpl = tmpl[key]
        for ph in placeholders:
            assert ph in tmpl, f"{'.'.join(path)} missing {ph}"
