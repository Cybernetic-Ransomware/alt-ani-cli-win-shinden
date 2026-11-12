"""Tests for player command builders — mpv and vlc."""

from unittest.mock import patch

import pytest

from alt_ani_cli.player import build_command, mpv
from alt_ani_cli.player.mpv import build as build_mpv
from alt_ani_cli.player.vlc import build as build_vlc
from alt_ani_core.models import StreamProtocol, StreamVariant


def _stream(**kw):
    defaults = dict(
        url="https://cdn.example.com/ep1.mp4",
        protocol=StreamProtocol.PROGRESSIVE,
        headers={"Referer": "https://shinden.pl/", "User-Agent": "TestUA"},
        container="mp4",
    )
    defaults.update(kw)
    return StreamVariant(**defaults)


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
        cmd = build_mpv(_stream(url="https://cdn.example.com/ep.mp4", headers={}), title="X")
        assert not any("--referrer" in arg for arg in cmd)

    def test_passes_through_extra_headers(self):
        stream = _stream(headers={"Referer": "https://shinden.pl/", "User-Agent": "TestUA", "Origin": "https://playmate.to"})
        cmd = build_mpv(stream, title="X")
        assert any(arg == "--http-header-fields=Origin: https://playmate.to" for arg in cmd)

    def test_no_extra_header_flag_when_only_standard_headers(self):
        cmd = build_mpv(_stream(), title="X")
        assert not any("--http-header-fields" in arg for arg in cmd)

    def test_escapes_comma_in_extra_header_value(self):
        stream = _stream(headers={"Referer": "https://shinden.pl/", "User-Agent": "TestUA", "Cookie": "a=1, b=2"})
        cmd = build_mpv(stream, title="X")
        assert any(arg == "--http-header-fields=Cookie: a=1\\, b=2" for arg in cmd)

    def test_extra_header_lookup_is_case_insensitive_for_standard_headers(self):
        stream = _stream(headers={"referer": "https://shinden.pl/", "user-agent": "TestUA"})
        cmd = build_mpv(stream, title="X")
        assert not any("--http-header-fields" in arg for arg in cmd)
        assert "--user-agent=TestUA" in cmd
        assert "--referrer=https://shinden.pl/" in cmd

    def test_includes_reconnect_hardening(self):
        cmd = build_mpv(_stream(), title="X")
        assert "--stream-lavf-o=reconnect=1,reconnect_streamed=1" in cmd

    def test_stream_url_is_passed_verbatim(self):
        cmd = build_mpv(_stream(url="https://cdn.example.com/master.m3u8?token=T", protocol=StreamProtocol.HLS), title="X")
        assert cmd[:2] == ["mpv.exe", "https://cdn.example.com/master.m3u8?token=T"]

    def test_log_file_lives_in_the_cli_cache_dir(self, cli_paths):
        assert mpv.log_file() == cli_paths.mpv_log
        assert cli_paths.mpv_log == cli_paths.cache_dir / "mpv-debug.log"

    def test_no_detach_writes_verbose_log_file(self, cli_paths):
        assert not cli_paths.cache_dir.exists()
        cmd = build_mpv(_stream(), title="X", no_detach=True)
        assert f"--log-file={cli_paths.mpv_log}" in cmd
        assert "--msg-level=all=v" in cmd
        assert cli_paths.cache_dir.is_dir()

    def test_detached_mode_also_writes_log_file(self, cli_paths):
        cmd = build_mpv(_stream(), title="X", no_detach=False)
        assert f"--log-file={cli_paths.mpv_log}" in cmd
        assert "--msg-level=all=v" in cmd
        assert cli_paths.cache_dir.is_dir()

    def test_no_detach_omits_no_terminal_flag(self):
        cmd = build_mpv(_stream(), title="X", no_detach=True)
        assert "--no-terminal" not in cmd

    def test_detached_mode_adds_no_terminal_flag(self):
        cmd = build_mpv(_stream(), title="X", no_detach=False)
        assert "--no-terminal" in cmd


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

    def test_stream_url_is_passed_verbatim(self):
        cmd = build_vlc(_stream(url="https://cdn.example.com/master.m3u8?token=T", protocol=StreamProtocol.HLS), title="X")
        assert cmd[:2] == ["vlc.exe", "https://cdn.example.com/master.m3u8?token=T"]

    def test_uses_variant_user_agent(self):
        assert "--http-user-agent=TestUA" in build_vlc(_stream(), title="X")


@pytest.mark.unit
class TestBuildCommand:
    def test_mpv_kind_builds_mpv_command_for_the_variant(self):
        variant = _stream()
        assert build_command("mpv", variant, title="X", no_detach=True) == build_mpv(variant, title="X", no_detach=True)

    def test_vlc_kind_builds_vlc_command_for_the_variant(self):
        variant = _stream()
        assert build_command("vlc", variant, title="X") == build_vlc(variant, title="X")
