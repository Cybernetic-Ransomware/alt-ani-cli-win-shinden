import pytest
from unittest.mock import patch

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


def test_mpv_includes_referrer():
    cmd = build_mpv(_stream(), title="TestAnime ep1")
    assert any("--referrer=https://shinden.pl/" in arg for arg in cmd)


def test_mpv_includes_title():
    cmd = build_mpv(_stream(), title="TestAnime ep1")
    assert any("TestAnime ep1" in arg for arg in cmd)


def test_mpv_no_referrer_when_absent():
    s = Stream(url="https://cdn.example.com/ep.mp4", headers={})
    cmd = build_mpv(s, title="X")
    assert not any("--referrer" in arg for arg in cmd)


def test_vlc_uses_http_referrer():
    cmd = build_vlc(_stream(), title="TestAnime ep1")
    assert any("--http-referrer=https://shinden.pl/" in arg for arg in cmd)


def test_vlc_uses_meta_title():
    cmd = build_vlc(_stream(), title="TestAnime ep1")
    assert any("--meta-title=TestAnime ep1" in arg for arg in cmd)


def test_vlc_play_and_exit():
    cmd = build_vlc(_stream(), title="X")
    assert "--play-and-exit" in cmd
