"""--debug rendering and playback reporting: secrets are redacted at display time only."""

import io
from unittest.mock import patch

import pytest
from alt_ani_core.models import EmbedURL, Stream, StreamVariant
from alt_ani_core.testing import make_stream
from rich.console import Console

from alt_ani_cli.args import CliOptions
from alt_ani_cli.playback import episode_title, playback_confirmed, print_debug, report_playback
from alt_ani_cli.player import mpv
from alt_ani_cli.player.runner import PlayResult

_URL = "https://cdn.example/x.mp4?token=SECRET"
_QUALITIES = {"1080p": "https://cdn.example/x1080.mp4?sig=ABC"}
_HEADERS = {
    "Authorization": "Bearer VERY_SECRET",
    "Cookie": "session=VERY_SECRET_TOO",
    "Referer": "https://embed.example/e/1?token=REF_SECRET",
    "User-Agent": "test-agent",
}
_EMBED = EmbedURL(url="https://embed.example/e/1?token=EMBED_SECRET", referer="https://shinden.pl/")


def _stream() -> Stream:
    return make_stream(_URL, headers=dict(_HEADERS), qualities=dict(_QUALITIES), ext="mp4")


def _render(stream: Stream, variant: StreamVariant | None = None) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=300, legacy_windows=False, highlight=False)
    with patch("alt_ani_cli.ui.progress._get", return_value=console):
        print_debug(stream, variant or stream.default, _EMBED)
    return buf.getvalue()


@pytest.mark.unit
class TestPrintDebugRedaction:
    def test_no_secret_in_output(self):
        out = _render(_stream())
        for secret in ("SECRET", "ABC", "VERY_SECRET", "VERY_SECRET_TOO", "REF_SECRET", "EMBED_SECRET", "Bearer"):
            assert secret not in out

    def test_hosts_and_paths_visible(self):
        out = _render(_stream())
        assert "cdn.example/x.mp4?token=<redacted>" in out
        assert "cdn.example/x1080.mp4?sig=<redacted>" in out
        assert "embed.example/e/1?token=<redacted>" in out
        assert "<redacted>" in out

    def test_user_agent_still_visible(self):
        assert "test-agent" in _render(_stream())


@pytest.mark.unit
class TestPrintDebugContent:
    def test_direct_url_is_the_chosen_variant_not_the_default(self):
        stream = _stream()
        out = _render(stream, stream.select("1080p"))
        assert "Direct URL: https://cdn.example/x1080.mp4?sig=<redacted>" in out
        assert "Direct URL: https://cdn.example/x.mp4" not in out

    def test_protocol_and_container_of_the_chosen_variant(self):
        out = _render(_stream())
        assert "Protocol: progressive" in out
        assert "Ext: mp4" in out

    def test_hls_variant_without_container_is_shown_as_m3u8(self):
        out = _render(make_stream("https://cdn.example/master.m3u8", ext="m3u8"))
        assert "Protocol: hls" in out
        assert "Ext: m3u8" in out

    def test_quality_table_lists_labelled_variants_only(self):
        out = _render(_stream())
        assert "Available qualities" in out
        assert "1080p" in out

    def test_no_tables_without_headers_and_qualities(self):
        out = _render(make_stream("https://cdn.example/plain.mp4"))
        assert "HTTP headers" not in out
        assert "Available qualities" not in out


@pytest.mark.unit
class TestRedactionLeavesRuntimeDataIntact:
    def test_print_debug_does_not_mutate_stream(self):
        stream = _stream()
        _render(stream)
        assert stream.default.url == _URL
        assert {v.label: v.url for v in stream.variants if v.label} == _QUALITIES
        assert all(v.headers == _HEADERS for v in stream.variants)
        assert _EMBED.url == "https://embed.example/e/1?token=EMBED_SECRET"

    def test_player_command_still_gets_raw_url_and_headers(self):
        stream = _stream()
        _render(stream)
        with patch("alt_ani_cli.player.mpv._find", return_value="mpv"):
            cmd = mpv.build(stream.default, title="t")
        assert _URL in cmd
        assert "--referrer=https://embed.example/e/1?token=REF_SECRET" in cmd
        fields = next(a for a in cmd if a.startswith("--http-header-fields="))
        assert "Authorization: Bearer VERY_SECRET" in fields
        assert "Cookie: session=VERY_SECRET_TOO" in fields


@pytest.mark.unit
class TestPlaybackConfirmed:
    def test_detached_playback_always_counts(self):
        assert playback_confirmed(PlayResult(rc=0, elapsed=0.0), no_detach=False) is True
        assert playback_confirmed(PlayResult(rc=1, elapsed=0.0), no_detach=False) is True

    def test_attached_playback_counts_after_the_fast_exit_threshold(self):
        assert playback_confirmed(PlayResult(rc=0, elapsed=2.0), no_detach=True) is True

    def test_attached_fast_exit_does_not_count(self):
        assert playback_confirmed(PlayResult(rc=0, elapsed=1.9), no_detach=True) is False

    def test_attached_failure_does_not_count(self):
        assert playback_confirmed(PlayResult(rc=2, elapsed=60.0), no_detach=True) is False


@pytest.mark.unit
class TestReportPlayback:
    _TITLE = "Fate — Odcinek 3"

    def test_detached_reports_success_and_mpv_log(self, console, cli_paths, diag_events):
        report_playback(PlayResult(rc=0, elapsed=0.0), CliOptions(), self._TITLE)

        assert console.of("success") == [f"Odtwarzam w mpv: {self._TITLE}"]
        assert any(str(cli_paths.mpv_log) in line for line in console.of("info"))
        assert diag_events == [
            (
                "playback_result",
                {"kind": "mpv", "rc": 0, "elapsed": "0.000", "confirmed": True, "mpv_log": str(cli_paths.mpv_log)},
            )
        ]

    def test_attached_nonzero_exit_is_an_error(self, console, diag_events):
        report_playback(PlayResult(rc=3, elapsed=10.0), CliOptions(no_detach=True), self._TITLE)

        assert console.of("success") == []
        (error,) = console.of("error")
        assert "kodem 3" in error
        assert self._TITLE in error
        assert diag_events[0][1]["confirmed"] is False

    def test_attached_fast_exit_is_unconfirmed(self, console, diag_events):
        report_playback(PlayResult(rc=0, elapsed=0.4), CliOptions(no_detach=True), self._TITLE)

        assert console.of("success") == []
        (warning,) = console.of("warn")
        assert "0.4s" in warning
        assert diag_events[0][1]["confirmed"] is False

    def test_attached_long_playback_is_a_success(self, console, diag_events):
        report_playback(PlayResult(rc=0, elapsed=30.0), CliOptions(no_detach=True), self._TITLE)

        assert console.of("success") == [f"Odtwarzam w mpv: {self._TITLE}"]
        assert console.of("error") == console.of("warn") == []
        assert diag_events[0][1]["confirmed"] is True

    def test_vlc_has_no_mpv_log_hint(self, console, diag_events):
        report_playback(PlayResult(rc=0, elapsed=0.0), CliOptions(vlc=True), self._TITLE)

        assert console.of("success") == [f"Odtwarzam w vlc: {self._TITLE}"]
        assert console.of("info") == []
        assert diag_events[0][1]["kind"] == "vlc"
        assert diag_events[0][1]["mpv_log"] is None


@pytest.mark.unit
class TestEpisodeTitle:
    @pytest.mark.parametrize(("number", "expected"), [(3.0, "Fate — Odcinek 3"), (12.5, "Fate — Odcinek 12.5")])
    def test_whole_numbers_drop_the_fraction(self, number, expected):
        assert episode_title("Fate", number) == expected
