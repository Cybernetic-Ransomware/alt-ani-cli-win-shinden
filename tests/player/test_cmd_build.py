"""Tests for player command builders — mpv and vlc."""

from unittest.mock import patch

import pytest

from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player.mpv import build as build_mpv
from alt_ani_cli.player.vlc import build as build_vlc


def _stream(**kw):
    defaults = dict(
        url="https://cdn.example.com/ep1.mp4",
        headers={"Referer": "https://shinden.pl/", "User-Agent": "TestUA"},
        ext="mp4",
    )
    defaults.update(kw)
    return Stream(**defaults)


@pytest.fixture(autouse=True)
def mock_find_player():
    with (
        patch("alt_ani_cli.player.mpv._find", return_value="mpv.exe"),
        patch("alt_ani_cli.player.vlc._find", return_value="vlc.exe"),
    ):
        yield


@pytest.mark.unit
class TestBuildMpv:
    def test_includes_referrer(self):
        cmd = build_mpv(_stream(), title="TestAnime ep1")
        assert any("--referrer=https://shinden.pl/" in arg for arg in cmd)

    def test_includes_title(self):
        cmd = build_mpv(_stream(), title="TestAnime ep1")
        assert any("TestAnime ep1" in arg for arg in cmd)

    def test_no_referrer_when_absent(self):
        cmd = build_mpv(Stream(url="https://cdn.example.com/ep.mp4", headers={}), title="X")
        assert not any("--referrer" in arg for arg in cmd)

    def test_passes_through_extra_headers(self):
        stream = _stream(headers={"Referer": "https://shinden.pl/", "User-Agent": "TestUA", "Origin": "https://playmate.to"})
        cmd = build_mpv(stream, title="X")
        assert any(arg == "--http-header-fields=Origin: https://playmate.to" for arg in cmd)

    def test_no_extra_header_flag_when_only_standard_headers(self):
        cmd = build_mpv(_stream(), title="X")
        assert not any("--http-header-fields" in arg for arg in cmd)

    def test_extra_header_lookup_is_case_insensitive_for_standard_headers(self):
        stream = _stream(headers={"referer": "https://shinden.pl/", "user-agent": "TestUA"})
        cmd = build_mpv(stream, title="X")
        assert not any("--http-header-fields" in arg for arg in cmd)

    def test_no_detach_writes_verbose_log_file(self, tmp_path):
        log_file = tmp_path / "mpv-debug.log"
        with (
            patch("alt_ani_cli.player.mpv.CACHE_DIR", tmp_path),
            patch("alt_ani_cli.player.mpv.LOG_FILE", log_file),
        ):
            cmd = build_mpv(_stream(), title="X", no_detach=True)
        assert f"--log-file={log_file}" in cmd
        assert "--msg-level=all=v" in cmd
        assert tmp_path.is_dir()

    def test_detached_mode_omits_log_file(self):
        cmd = build_mpv(_stream(), title="X", no_detach=False)
        assert not any("--log-file" in arg for arg in cmd)
        assert not any("--msg-level" in arg for arg in cmd)


@pytest.mark.unit
class TestFindMpv:
    # Overrides the module-level autouse fixture, which mocks out _find() itself —
    # these tests exercise the real _find() implementation.
    @pytest.fixture(autouse=True)
    def mock_find_player(self):
        yield

    def test_no_detach_prefers_mpv_com(self, monkeypatch):
        from alt_ani_cli.player.mpv import _find

        monkeypatch.delenv("ANI_CLI_PLAYER", raising=False)
        calls: list[str] = []

        def _which(name):
            calls.append(name)
            return f"C:\\{name}" if name == "mpv.com" else None

        with patch("alt_ani_cli.player.mpv.shutil.which", side_effect=_which):
            path = _find(no_detach=True)
        assert path == "C:\\mpv.com"
        assert calls[0] == "mpv.com"

    def test_detached_prefers_mpv_exe(self, monkeypatch):
        from alt_ani_cli.player.mpv import _find

        monkeypatch.delenv("ANI_CLI_PLAYER", raising=False)
        calls: list[str] = []

        def _which(name):
            calls.append(name)
            return f"C:\\{name}" if name == "mpv.exe" else None

        with patch("alt_ani_cli.player.mpv.shutil.which", side_effect=_which):
            path = _find(no_detach=False)
        assert path == "C:\\mpv.exe"
        assert calls[0] == "mpv.exe"


@pytest.mark.unit
class TestBuildVlc:
    def test_uses_http_referrer(self):
        cmd = build_vlc(_stream(), title="TestAnime ep1")
        assert any("--http-referrer=https://shinden.pl/" in arg for arg in cmd)

    def test_uses_meta_title(self):
        cmd = build_vlc(_stream(), title="TestAnime ep1")
        assert any("--meta-title=TestAnime ep1" in arg for arg in cmd)

    def test_play_and_exit(self):
        assert "--play-and-exit" in build_vlc(_stream(), title="X")
