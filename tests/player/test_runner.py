"""Tests for player/runner.py — subprocess launch and PlayResult reporting."""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.player.runner import play
from alt_ani_core.models import StreamProtocol, StreamVariant


def _stream():
    return StreamVariant(url="https://cdn.example.com/v.m3u8", protocol=StreamProtocol.HLS)


@pytest.mark.unit
class TestPlay:
    def test_no_detach_returns_real_rc_and_measured_elapsed(self):
        with (
            patch("alt_ani_cli.player.runner.build_command", return_value=["mpv", "url"]) as build,
            patch("alt_ani_cli.player.runner.subprocess.run", return_value=MagicMock(returncode=7)) as run,
            patch("alt_ani_cli.player.runner.time.monotonic", side_effect=[10.0, 13.5]),
        ):
            result = play(_stream(), kind="mpv", title="X", no_detach=True)
        build.assert_called_once_with("mpv", _stream(), title="X", no_detach=True)
        run.assert_called_once_with(["mpv", "url"])
        assert result.rc == 7
        assert result.elapsed == 3.5

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_detached_returns_rc_zero_elapsed_zero_without_waiting(self, platform):
        with (
            patch("alt_ani_cli.player.runner.build_command", return_value=["mpv", "url"]),
            patch("alt_ani_cli.player.runner.subprocess.Popen") as mock_popen,
            patch("alt_ani_cli.player.runner.subprocess.run") as run,
            patch("alt_ani_cli.player.runner.sys", SimpleNamespace(platform=platform)),
            patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch.object(subprocess, "DETACHED_PROCESS", 0x8, create=True),
        ):
            result = play(_stream(), kind="mpv", title="X", no_detach=False)
        assert result.rc == 0
        assert result.elapsed == 0.0
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args == (["mpv", "url"],)
        # the player must outlive the terminal and never write into it
        assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
        if platform == "win32":
            assert kwargs["creationflags"] == 0x200 | 0x8 and "start_new_session" not in kwargs
        else:
            assert kwargs["start_new_session"] is True and "creationflags" not in kwargs
        run.assert_not_called()
        mock_popen.return_value.wait.assert_not_called()
        mock_popen.return_value.communicate.assert_not_called()
