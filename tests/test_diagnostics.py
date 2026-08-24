"""Tests for the local session diagnostics log — redaction and file lifecycle."""

import logging
import os
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
        assert diagnostics._safe_value("line1\nline2\tend") == '"line1 line2 end"'

    def test_collapses_repeated_whitespace(self):
        assert diagnostics._safe_value("a    b") == '"a b"'

    def test_quotes_value_containing_space(self):
        assert diagnostics._safe_value("Attack on Titan") == '"Attack on Titan"'

    def test_no_quotes_for_single_token(self):
        assert diagnostics._safe_value("mp4upload.com") == "mp4upload.com"


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

    def test_all_public_events_can_be_emitted_without_raising(self, log_path):
        diagnostics.session_start("1.0.0", "3.14.0", "Windows")
        diagnostics.series_selected("s1", "Test Series")
        diagnostics.episode_selected(1.0, "Episode One")
        diagnostics.player_selected("p1", "mp4upload", "mp4upload.com")
        diagnostics.resolve_result("mp4upload.com", True, None, 0.5)
        diagnostics.playback_result("mpv", 0, 10.0, True, None)
        diagnostics.history_update("s1", 1.0)
        diagnostics.session_end("ok", None)

        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 8


@pytest.mark.unit
class TestNoOpWithoutConfigure:
    def test_events_are_safe_when_not_configured(self, capsys):
        diagnostics.series_selected("s1", "Test Series")
        diagnostics.resolve_result("host.example.com", False, "NoStreamError", 1.0)
        diagnostics.session_end("ok", None)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
