"""Tests for player/runner.py — subprocess launch and PlayResult reporting."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player.runner import play


def _stream():
    return Stream(url="https://cdn.example.com/v.m3u8", headers={}, ext="m3u8")


@pytest.mark.unit
class TestPlay:
    def test_no_detach_returns_real_rc_and_measured_elapsed(self):
        with (
            patch("alt_ani_cli.player.runner.build_command", return_value=["mpv", "url"]),
            patch("alt_ani_cli.player.runner.subprocess.run", return_value=MagicMock(returncode=7)),
        ):
            result = play(_stream(), kind="mpv", title="X", no_detach=True)
        assert result.rc == 7
        assert result.elapsed >= 0.0

    def test_detached_returns_rc_zero_elapsed_zero_without_waiting(self):
        with (
            patch("alt_ani_cli.player.runner.build_command", return_value=["mpv", "url"]),
            patch("alt_ani_cli.player.runner.subprocess.Popen") as mock_popen,
        ):
            result = play(_stream(), kind="mpv", title="X", no_detach=False)
        assert result.rc == 0
        assert result.elapsed == 0.0
        mock_popen.assert_called_once()
