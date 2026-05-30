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
    with patch("alt_ani_cli.player.mpv._find", return_value="mpv.exe"), \
         patch("alt_ani_cli.player.vlc._find", return_value="vlc.exe"):
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
