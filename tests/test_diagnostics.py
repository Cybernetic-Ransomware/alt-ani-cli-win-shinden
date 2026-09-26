"""Tests for the local session diagnostics log — redaction and file lifecycle."""

import logging
import os
import shlex
from unittest.mock import patch

import pytest

from alt_ani_cli import diagnostics


@pytest.fixture(autouse=True)
def _reset_diagnostics_logger():
    """Every test starts from a NullHandler-only logger, regardless of what ran before it."""
    yield
    for handler in [h for h in diagnostics._logger.handlers if not isinstance(h, logging.NullHandler)]:
        diagnostics._logger.removeHandler(handler)
        handler.close()


@pytest.mark.unit
class TestConfigure:
    def test_creates_session_log_under_diag_dir(self, tmp_path):
        with patch("alt_ani_cli.diagnostics.DIAG_DIR", tmp_path):
            path = diagnostics.configure()
        assert path.exists()
        assert path.parent == tmp_path
        assert path.name.startswith("session_")
        assert path.suffix == ".log"


@pytest.mark.unit
class TestConfigureIdempotent:
    def test_second_configure_stops_writing_to_first_file(self, tmp_path):
        with patch("alt_ani_cli.diagnostics.DIAG_DIR", tmp_path):
            first = diagnostics.configure()
            second = diagnostics.configure()
            diagnostics.series_selected("123", "Test Series")

        assert first != second
        assert "series_selected" not in first.read_text(encoding="utf-8")
        assert "series_selected" in second.read_text(encoding="utf-8")


@pytest.mark.unit
class TestPruning:
    def test_keeps_at_most_max_sessions_files_including_the_new_one(self, tmp_path):
        for i in range(5):
            f = tmp_path / f"session_old_{i}.log"
            f.write_text("stale", encoding="utf-8")
            os.utime(f, (i, i))

        with patch("alt_ani_cli.diagnostics.DIAG_DIR", tmp_path), patch("alt_ani_cli.diagnostics._MAX_SESSIONS", 3):
            path = diagnostics.configure()

        remaining = {p.name for p in tmp_path.glob("session_*.log")}
        assert remaining == {"session_old_3.log", "session_old_4.log", path.name}


@pytest.mark.unit
class TestHostOfUrl:
    def test_strips_userinfo_port_path_query(self):
        url = "https://user:token@evilcdn.example.com:8443/path?token=abc123"
        assert diagnostics._host_of_url(url) == "evilcdn.example.com"

    def test_strips_www_prefix(self):
        assert diagnostics._host_of_url("https://www.mp4upload.com/embed-xyz.html") == "mp4upload.com"

    def test_falls_back_to_unknown_host_for_garbage_input(self):
        assert diagnostics._host_of_url("not a url") == "unknown-host"

    def test_falls_back_to_unknown_host_when_hostname_missing(self):
        assert diagnostics._host_of_url("/relative/path") == "unknown-host"


@pytest.mark.unit
class TestSafeValue:
    def test_collapses_newlines_and_tabs_to_single_space(self):
        # multi-word after collapsing, so it also gets quoted like any other value with a space
        assert diagnostics._safe_value("line1\nline2\tend") == "'line1 line2 end'"

    def test_collapses_repeated_whitespace(self):
        assert diagnostics._safe_value("a    b") == "'a b'"

    def test_quotes_value_containing_space(self):
        assert diagnostics._safe_value("Attack on Titan") == "'Attack on Titan'"

    def test_no_quotes_for_single_token(self):
        assert diagnostics._safe_value("mp4upload.com") == "mp4upload.com"


@pytest.mark.unit
class TestSafeValueRoundTrip:
    """The replay harness reads these logs back with shlex.split — the exact original value must survive."""

    @pytest.mark.parametrize(
        "original",
        [
            "Bob's",
            'He said "Hi"',
            r"C:\Users\Scorpos\AppData\Local\Temp",
            "Attack on Titan",
        ],
        ids=["apostrophe", "double_quotes", "windows_path", "spaces"],
    )
    def test_round_trips_through_shlex_split(self, original):
        token = f"key={diagnostics._safe_value(original)}"
        (parsed,) = shlex.split(token)
        key, _, value = parsed.partition("=")
        assert key == "key"
        assert value == original


@pytest.mark.unit
class TestEventEmission:
    @pytest.fixture
    def log_path(self, tmp_path):
        with patch("alt_ani_cli.diagnostics.DIAG_DIR", tmp_path):
            return diagnostics.configure()

    def test_resolve_result_failure_records_host_and_exception_class_without_full_url(self, log_path):
        secret_url = "https://evilcdn.example.com/embed/abc?token=SUPERSECRETTOKEN123"
        diagnostics.resolve_result(diagnostics._host_of_url(secret_url), False, "NoStreamError", 1.23)

        content = log_path.read_text(encoding="utf-8")
        assert "host=evilcdn.example.com" in content
        assert "exc=NoStreamError" in content
        assert "ok=False" in content
        assert secret_url not in content
        assert "SUPERSECRETTOKEN123" not in content
        assert "http://" not in content
        assert "https://" not in content

    def test_resolve_result_failure_records_layer_category_status_and_fallback(self, log_path):
        diagnostics.resolve_result(
            "vidawra.cc",
            False,
            "NoStreamError",
            6.92,
            layer="jwplayer",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="network_error",
            fallback_http_status=None,
        )

        content = log_path.read_text(encoding="utf-8")
        assert " layer=jwplayer" in content
        assert " category=http_error" in content
        assert " http_status=403" in content
        assert "used_fallback=True" in content
        assert "fallback_layer=ytdlp" in content
        assert "fallback_category=network_error" in content
        assert "fallback_http_status" not in content

    def test_resolve_result_omits_diagnostic_fields_when_not_given(self, log_path):
        diagnostics.resolve_result("mp4upload.com", True, None, 0.5)

        content = log_path.read_text(encoding="utf-8")
        assert "layer=" not in content
        assert "category=" not in content
        assert "http_status=" not in content
        assert "used_fallback=" not in content

    def test_playback_result_includes_local_mpv_log_path_but_no_urls(self, log_path, tmp_path):
        mpv_log = tmp_path / "mpv-debug.log"
        diagnostics.playback_result(kind="mpv", rc=0, elapsed=5.0, confirmed=True, mpv_log=str(mpv_log))

        content = log_path.read_text(encoding="utf-8")
        assert "kind=mpv" in content
        assert "confirmed=True" in content
        assert str(mpv_log) in content
        assert "https://" not in content

    def test_session_start_records_environment_without_urls(self, log_path):
        diagnostics.session_start("1.2.3", "3.14.0", "Windows-11-10.0.26200")

        content = log_path.read_text(encoding="utf-8")
        assert "version=1.2.3" in content
        assert "python=3.14.0" in content
        assert "https://" not in content

    def test_session_start_without_mode_omits_field(self, log_path):
        diagnostics.session_start("1.0.0", "3.14.0", "Windows")

        content = log_path.read_text(encoding="utf-8")
        assert "mode=" not in content

    @pytest.mark.parametrize("mode", ["interactive", "noninteractive"])
    def test_session_start_records_mode(self, log_path, mode):
        diagnostics.session_start("1.0.0", "3.14.0", "Windows", mode=mode)

        content = log_path.read_text(encoding="utf-8")
        assert f"mode={mode}" in content

    def test_host_health_records_states_and_context_without_urls(self, log_path):
        secret_url = "https://evilcdn.example.com/embed/abc?token=SUPERSECRETTOKEN123"
        diagnostics.host_health(
            host=diagnostics._host_of_url(secret_url),
            from_state="degraded",
            to_state="unavailable",
            signal="transient",
            online_id="123",
            category="timeout",
            http_status=None,
            resolver="vidara",
            evidence_expired=False,
        )

        content = log_path.read_text(encoding="utf-8")
        assert "host=evilcdn.example.com" in content
        assert "from_state=degraded" in content
        assert "to_state=unavailable" in content
        assert "signal=transient" in content
        assert "online_id=123" in content
        assert "category=timeout" in content
        assert "resolver=vidara" in content
        assert "evidence_expired=False" in content
        assert "http_status=" not in content
        assert secret_url not in content
        assert "SUPERSECRETTOKEN123" not in content
        assert "http://" not in content
        assert "https://" not in content

    def test_host_health_bool_and_apostrophe_values_round_trip(self, log_path):
        diagnostics.host_health(
            host="h.com",
            from_state="healthy",
            to_state="degraded",
            signal="soft",
            online_id="1",
            category="parser_drift",
            http_status=None,
            resolver="Bob's Resolver",
            evidence_expired=True,
        )

        line = log_path.read_text(encoding="utf-8").splitlines()[0]
        assert "evidence_expired=True" in line
        _date, _time, rest = line.split(" ", 2)
        (resolver_token,) = [t for t in shlex.split(rest) if t.startswith("resolver=")]
        assert resolver_token.partition("=")[2] == "Bob's Resolver"

    @pytest.mark.parametrize("action", ["deferred", "retry", "dropped"])
    def test_health_defer_action_values_round_trip(self, log_path, action):
        diagnostics.health_defer(
            action=action,
            host="vidawra.cc",
            online_id="123",
            player="vidara",
            state="unavailable",
            category="timeout",
            http_status=None,
        )

        content = log_path.read_text(encoding="utf-8")
        assert f"action={action}" in content

    def test_health_defer_records_context_without_urls(self, log_path):
        diagnostics.health_defer(
            action="deferred",
            host="vidawra.cc",
            online_id="123",
            player="vidara",
            state="unavailable",
            category="timeout",
            http_status=None,
        )

        content = log_path.read_text(encoding="utf-8")
        assert "host=vidawra.cc" in content
        assert "online_id=123" in content
        assert "player=vidara" in content
        assert "state=unavailable" in content
        assert "category=timeout" in content
        assert "http_status=" not in content
        assert "https://" not in content

    def test_health_defer_omits_none_http_status(self, log_path):
        diagnostics.health_defer(
            action="retry", host="h.com", online_id="1", player="p", state="degraded", category="timeout", http_status=None
        )

        content = log_path.read_text(encoding="utf-8")
        assert "http_status=" not in content

    def test_all_public_events_can_be_emitted_without_raising(self, log_path):
        diagnostics.session_start("1.0.0", "3.14.0", "Windows", mode="interactive")
        diagnostics.series_selected("s1", "Test Series")
        diagnostics.episode_selected(1.0, "Episode One")
        diagnostics.player_selected("p1", "mp4upload", "mp4upload.com")
        diagnostics.resolve_result("mp4upload.com", True, None, 0.5)
        diagnostics.host_health(
            host="mp4upload.com",
            from_state="healthy",
            to_state="degraded",
            signal="soft",
            online_id="p1",
            category="parser_drift",
            http_status=None,
            resolver="mp4upload",
            evidence_expired=False,
        )
        diagnostics.health_defer(
            action="deferred",
            host="mp4upload.com",
            online_id="p1",
            player="mp4upload",
            state="degraded",
            category="parser_drift",
            http_status=None,
        )
        diagnostics.playback_result("mpv", 0, 10.0, True, None)
        diagnostics.history_update("s1", 1.0)
        diagnostics.session_end("ok", None)

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10


@pytest.mark.unit
class TestNoOpWithoutConfigure:
    def test_events_are_safe_when_not_configured(self, capsys):
        diagnostics.series_selected("s1", "Test Series")
        diagnostics.resolve_result("host.example.com", False, "NoStreamError", 1.0)
        diagnostics.session_end("ok", None)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
