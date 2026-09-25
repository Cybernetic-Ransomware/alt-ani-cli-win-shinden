"""Tests for cli._resolve_with_fallback — diagnostics fields passed through on failure."""

from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.cli import _resolve_with_fallback
from alt_ani_cli.errors import AntiBotError, NoStreamError
from alt_ani_cli.models import EmbedURL, PlayerEntry

_PLAYER = PlayerEntry(online_id="123", player="Default", lang_audio="pl", lang_subs="pl")
_EMBED = EmbedURL(url="https://vidawra.cc/e/abc123", referer="https://shinden.pl/")


def _tagged_no_stream_error(*, layer, category, http_status, used_fallback) -> NoStreamError:
    err = NoStreamError("all extractors failed")
    err.layer = layer
    err.category = category
    err.http_status = http_status
    err.used_fallback = used_fallback
    return err


@pytest.mark.unit
class TestResolveWithFallbackDiagnostics:
    def test_success_records_ok_result(self):
        stream = MagicMock()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0)
        assert result is stream
        assert embed is _EMBED
        mock_diag.assert_called_once()
        args, kwargs = mock_diag.call_args
        assert args[0] == "vidawra.cc"
        assert args[1] is True

    def test_extractor_failure_forwards_layer_category_status_and_fallback(self):
        exc = _tagged_no_stream_error(layer="jwplayer", category="http_error", http_status=403, used_fallback=True)
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=exc),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0)
        assert result is None
        assert embed is None
        mock_diag.assert_called_once()
        args, kwargs = mock_diag.call_args
        assert args[0] == "vidawra.cc"
        assert args[1] is False
        assert args[2] == "NoStreamError"
        assert kwargs["layer"] == "jwplayer"
        assert kwargs["category"] == "http_error"
        assert kwargs["http_status"] == 403
        assert kwargs["used_fallback"] is True

    def test_untagged_exception_forwards_none_fields(self):
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=NoStreamError("boom")),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_diag,
        ):
            _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0)
        _, kwargs = mock_diag.call_args
        assert kwargs["layer"] is None
        assert kwargs["category"] is None
        assert kwargs["http_status"] is None
        assert kwargs["used_fallback"] is None

    def test_antibot_error_from_embed_resolution_tags_shinden_layer(self):
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("guest token expired")),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0)
        assert result is None
        assert embed is None
        _, kwargs = mock_diag.call_args
        args = mock_diag.call_args.args
        assert args[0] is None
        assert args[2] == "AntiBotError"
        assert kwargs["layer"] == "shinden_api"
        assert kwargs["category"] == "anti_bot"
