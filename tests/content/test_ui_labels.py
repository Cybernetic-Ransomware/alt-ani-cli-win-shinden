"""Text contract of rendered UI labels.

These tests pin the label *format* (stable technical tokens: id, date, counts,
watched marker distinguishability, template placeholders) — not menu behaviour,
which lives in tests/ui/test_menus.py.
"""

import inspect
import re
from importlib import resources
from string import Formatter
from unittest.mock import patch

import pytest
import yaml
from alt_ani_core import events
from alt_ani_core.errors import DownloadTargetError, ParseError, ShindenError
from alt_ani_core.messages import MESSAGES, text
from alt_ani_core.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesMetadata

from alt_ani_cli import content, event_renderer
from alt_ani_cli.content import CONTENT, EXCEPTIONS, EXCEPTIONS_PL, error_text, load_content
from alt_ani_cli.ui.menus import _player_label, _player_origin, _series_label, select_episodes, select_start_mode

_HIT = SeriesHit(
    id="234", slug="ikkitousen", title="Ikkitousen", url="https://shinden.pl/series/234-ikkitousen", series_type="TV"
)
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

    def test_downloaded_template_differs_from_watched_and_unwatched(self):
        _ep = CONTENT["menu"]["episodes"]
        downloaded = _ep["label_downloaded"].format(number=1.0, title="Ep A")
        assert downloaded != _ep["label_watched"].format(number=1.0, title="Ep A")
        assert downloaded != _ep["label_unwatched"].format(number=1.0, title="Ep A")

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
            select_start_mode(watch_count=5)
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
            (("menu", "start_mode", "options", "resume_watch"), ("{count}",)),
            (("menu", "start_mode", "options", "resume_download"), ("{count}",)),
            (("menu", "download_resume", "label"), ("{title}", "{count}")),
            (("menu", "episodes", "label_downloaded"), ("{number", "{title}")),
            (("progress", "downloaded_all"), ("{title}",)),
            (("menu", "start_mode", "version_body"), ("{version}",)),
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
            (("progress", "host_deferred"), ("{host}",)),
            (("progress", "host_last_resort"), ("{host}",)),
            (("progress", "host_dropped"), ("{host}",)),
            (("progress", "prefetch_stopped"), ("{reason}",)),
            (("progress", "network_error"), ("{exc}",)),
        ],
    )
    def test_template_has_required_placeholders(self, path, placeholders):
        tmpl = CONTENT
        for key in path:
            tmpl = tmpl[key]
        for ph in placeholders:
            assert ph in tmpl, f"{'.'.join(path)} missing {ph}"


_API_PLACEHOLDERS = {
    "xhr_failed": ("{stage}", "{online_id}", "{detail}"),
    "xhr_access_blocked": ("{stage}", "{detail}"),
    "xhr_unavailable": ("{stage}", "{detail}"),
    "xhr_protocol": ("{stage}", "{detail}"),
    "retry_after_hint": ("{seconds}",),
    "no_iframe": ("{online_id}", "{length}"),
    "unexpected_iframe_src": ("{online_id}",),
}


@pytest.mark.unit
class TestApiExceptionTemplates:
    @pytest.mark.parametrize("catalog", [EXCEPTIONS, EXCEPTIONS_PL], ids=["en-us", "pl-pl"])
    @pytest.mark.parametrize(("key", "placeholders"), list(_API_PLACEHOLDERS.items()))
    def test_template_has_required_placeholders(self, catalog, key, placeholders):
        for ph in placeholders:
            assert ph in catalog["api"][key], f"api.{key} missing {ph}"

    @pytest.mark.parametrize("catalog", [EXCEPTIONS, EXCEPTIONS_PL], ids=["en-us", "pl-pl"])
    def test_no_iframe_no_longer_embeds_a_response_snippet(self, catalog):
        assert "{snippet}" not in catalog["api"]["no_iframe"]

    @pytest.mark.parametrize("catalog", [EXCEPTIONS, EXCEPTIONS_PL], ids=["en-us", "pl-pl"])
    def test_unexpected_iframe_src_does_not_embed_the_src(self, catalog):
        assert "{src}" not in catalog["api"]["unexpected_iframe_src"]


def _packaged_yaml(file_name: str):
    return yaml.safe_load(resources.files("alt_ani_cli.content").joinpath(file_name).read_text(encoding="utf-8"))


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


@pytest.mark.unit
class TestContentLoading:
    @pytest.mark.parametrize(
        ("file_name", "loaded"),
        [("ui_content_pl-pl.yaml", CONTENT), ("exceptions_content_pl-pl.yaml", EXCEPTIONS_PL)],
    )
    def test_catalog_is_the_packaged_yaml_resource(self, file_name, loaded):
        assert load_content(file_name) == loaded == _packaged_yaml(file_name)

    def test_loads_through_package_resources_not_the_working_directory(self, tmp_path, monkeypatch):
        (tmp_path / "ui_content_pl-pl.yaml").write_text("progress: {}", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert load_content("ui_content_pl-pl.yaml") == CONTENT

    def test_missing_resource_raises(self):
        with pytest.raises(FileNotFoundError):
            load_content("no_such_content.yaml")

    def test_cli_owned_english_file_holds_only_player_texts(self):
        assert set(_packaged_yaml("exceptions_content_en-us.yaml")) == {"player"}

    def test_exceptions_are_engine_messages_plus_player_section(self):
        assert {**MESSAGES, "player": _packaged_yaml("exceptions_content_en-us.yaml")["player"]} == EXCEPTIONS
        assert "player" not in MESSAGES

    def test_engine_messages_are_not_overridden_by_the_cli(self):
        for section, templates in MESSAGES.items():
            assert EXCEPTIONS[section] == templates

    def test_content_has_the_sections_the_cli_reads(self):
        assert {"progress", "download", "menu"} <= set(CONTENT)


@pytest.mark.unit
class TestExceptionCatalogParity:
    def test_same_sections(self):
        assert set(EXCEPTIONS_PL) == set(EXCEPTIONS)

    @pytest.mark.parametrize("section", sorted(EXCEPTIONS))
    def test_same_keys_in_section(self, section):
        assert set(EXCEPTIONS_PL[section]) == set(EXCEPTIONS[section])

    @pytest.mark.parametrize(
        ("section", "key"), [(section, key) for section in sorted(EXCEPTIONS) for key in sorted(EXCEPTIONS[section])]
    )
    def test_same_placeholders(self, section, key):
        assert _fields(EXCEPTIONS_PL[section][key]) == _fields(EXCEPTIONS[section][key])

    @pytest.mark.parametrize("section", sorted(MESSAGES))
    def test_every_engine_message_has_a_polish_counterpart(self, section):
        assert set(MESSAGES[section]) <= set(EXCEPTIONS_PL[section])


@pytest.mark.unit
class TestErrorText:
    def test_download_error_is_rendered_in_polish(self):
        exc = DownloadTargetError(text("download.replace_failed", path="x.mp4")).coded("download.replace_failed", path="x.mp4")
        assert error_text(exc) == EXCEPTIONS_PL["download"]["replace_failed"].format(path="x.mp4")

    def test_age_gate_is_rendered_in_polish(self):
        exc = ParseError(text("series.age_gate")).coded("series.age_gate")
        assert error_text(exc) == EXCEPTIONS_PL["series"]["age_gate"]
        assert error_text(exc) != str(exc)

    def test_other_coded_errors_keep_the_engine_text(self):
        message = text("shinden.parser_drift", page="series", detail="no title")
        exc = ParseError(message).coded("shinden.parser_drift", page="series", detail="no title")
        assert error_text(exc) == message

    def test_other_series_codes_are_not_localized(self):
        message = text("series.parse_error", url="shinden.pl/x")
        assert error_text(ParseError(message).coded("series.parse_error", url="shinden.pl/x")) == message

    def test_uncoded_error_is_rendered_from_its_own_text(self):
        assert error_text(ShindenError("plain failure")) == "plain failure"

    def test_unknown_localized_key_falls_back_to_the_engine_text(self):
        assert error_text(ShindenError("engine text").coded("download.not_in_catalog")) == "engine text"

    def test_rendered_text_is_redacted(self):
        exc = ShindenError("failed for https://cdn.example/master.m3u8?token=SECRET")
        assert "SECRET" not in error_text(exc)
        assert "<redacted>" in error_text(exc)

    def test_localized_text_is_redacted(self):
        path = "https://cdn.example/master.m3u8?token=SECRET"
        exc = DownloadTargetError("x").coded("download.replace_failed", path=path)
        assert "SECRET" not in error_text(exc)

    def test_localized_sections_exist_in_the_polish_catalog(self):
        for section in content._LOCALIZED_SECTIONS:
            assert set(EXCEPTIONS_PL[section]) == set(MESSAGES[section])
        for code in content._LOCALIZED_CODES:
            section, key = code.split(".", 1)
            assert key in EXCEPTIONS_PL[section]


_RENDERER_SOURCE = inspect.getsource(event_renderer)
_FALLBACK_KINDS = ("extractor_fallback", "jwplayer_fallback")


@pytest.mark.unit
class TestEventRendererContentKeys:
    def test_renderer_reads_progress_and_download_sections(self):
        assert event_renderer._PROG is CONTENT["progress"]
        assert event_renderer._DL is CONTENT["download"]

    @pytest.mark.parametrize("key", sorted(set(re.findall(r'_PROG\["(\w+)"\]', _RENDERER_SOURCE))))
    def test_progress_key_exists(self, key):
        assert isinstance(CONTENT["progress"][key], str)

    @pytest.mark.parametrize("key", sorted(set(re.findall(r'_DL\["(\w+)"\]', _RENDERER_SOURCE))))
    def test_download_key_exists(self, key):
        assert isinstance(CONTENT["download"][key], str)

    def test_static_key_scan_finds_the_known_keys(self):
        assert set(re.findall(r'_PROG\["(\w+)"\]', _RENDERER_SOURCE)) >= {
            "spinner_api",
            "clearance_requested",
            "xhr_fallback_api4",
            "embed",
            "player_failed_long",
            "host_deferred",
            "host_last_resort",
            "host_dropped",
        }
        assert set(re.findall(r'_DL\["(\w+)"\]', _RENDERER_SOURCE)) >= {"starting", "legacy_part_moved", "saved"}

    @pytest.mark.parametrize("kind", _FALLBACK_KINDS)
    def test_extractor_fallback_kind_is_a_progress_key(self, kind):
        assert _fields(CONTENT["progress"][kind]) == {"host", "exc"}

    def test_fallback_kinds_match_what_the_engine_emits(self):
        from alt_ani_core import extract

        emitted = set(re.findall(r'on_fallback\("(\w+)"', inspect.getsource(extract)))
        assert emitted == set(_FALLBACK_KINDS)

    @pytest.mark.parametrize(
        ("section", "key", "fields"),
        [
            ("progress", "spinner_api", {"label"}),
            ("progress", "clearance_requested", set()),
            ("progress", "xhr_fallback_api4", {"number", "reason"}),
            ("progress", "embed", {"url"}),
            ("progress", "player_failed_long", {"player", "number", "exc"}),
            ("progress", "host_deferred", {"host"}),
            ("progress", "host_last_resort", {"host"}),
            ("progress", "host_dropped", {"host"}),
            ("download", "starting", {"title", "ep_label", "dir"}),
            ("download", "legacy_part_moved", {"path"}),
            ("download", "saved", {"path"}),
        ],
    )
    def test_template_takes_exactly_the_fields_the_renderer_passes(self, section, key, fields):
        assert _fields(CONTENT[section][key]) == fields

    def test_every_engine_event_type_is_known_to_this_suite(self):
        declared = {
            cls.__name__
            for cls in vars(events).values()
            if isinstance(cls, type) and issubclass(cls, events.EngineEvent) and cls is not events.EngineEvent
        }
        rendered = set(re.findall(r"case ev\.(\w+)\(", _RENDERER_SOURCE))
        assert rendered <= declared
        assert declared - rendered == {"ClearanceUnavailable"}
