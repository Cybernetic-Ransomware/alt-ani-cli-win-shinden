"""Tests for cli._resolve_with_fallback — diagnostics fields passed through on failure."""

from unittest.mock import MagicMock, call, patch

import pytest

from alt_ani_cli.cli import _resolve_with_fallback
from alt_ani_cli.errors import AntiBotError, NoStreamError
from alt_ani_cli.health import HostState, ResolverHealth, Signal
from alt_ani_cli.models import EmbedURL, PlayerEntry

_PLAYER = PlayerEntry(online_id="123", player="Default", lang_audio="pl", lang_subs="pl")
_EMBED = EmbedURL(url="https://vidawra.cc/e/abc123", referer="https://shinden.pl/")


def _tagged_no_stream_error(**fields) -> NoStreamError:
    err = NoStreamError("all extractors failed")
    for name, value in fields.items():
        setattr(err, name, value)
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
        exc = _tagged_no_stream_error(
            layer="jwplayer",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="timeout",
            fallback_http_status=None,
        )
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
        assert kwargs["fallback_layer"] == "ytdlp"
        assert kwargs["fallback_category"] == "timeout"
        assert kwargs["fallback_http_status"] is None

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
        assert kwargs["fallback_layer"] is None
        assert kwargs["fallback_category"] is None

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


@pytest.mark.unit
class TestResolveWithFallbackHealthObservationOnly:
    """health=None keeps today's behavior; health=ResolverHealth() only ever records, never decides."""

    def test_health_none_preserves_existing_behavior(self):
        stream = MagicMock()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream),
            patch("alt_ani_cli.cli.diagnostics.host_health") as mock_health_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0, health=None)
        assert result is stream
        assert embed is _EMBED
        mock_health_diag.assert_not_called()

    def test_success_records_health_and_emits_host_health(self):
        stream = MagicMock()
        health = ResolverHealth()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.host_health") as mock_health_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0, health=health)

        assert result is stream
        assert health.state("vidawra.cc") == HostState.HEALTHY
        mock_result.assert_called_once()
        mock_health_diag.assert_called_once()
        _, kwargs = mock_health_diag.call_args
        assert kwargs["host"] == "vidawra.cc"
        assert kwargs["from_state"] == "unknown"
        assert kwargs["to_state"] == "healthy"
        assert kwargs["signal"] == "success"
        assert kwargs["online_id"] == _PLAYER.online_id
        assert kwargs["category"] is None
        assert kwargs["http_status"] is None
        assert kwargs["resolver"] == "vidara"
        assert kwargs["evidence_expired"] is False

    def test_no_stream_error_classifies_and_emits_host_health(self):
        exc = _tagged_no_stream_error(
            layer="jwplayer",
            category="http_error",
            http_status=403,
            used_fallback=False,
            fallback_layer=None,
            fallback_category=None,
            fallback_http_status=None,
        )
        health = ResolverHealth()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=exc),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.host_health") as mock_health_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0, health=health)

        assert result is None
        assert health.state("vidawra.cc") == HostState.DEGRADED
        mock_result.assert_called_once()
        mock_health_diag.assert_called_once()
        _, kwargs = mock_health_diag.call_args
        assert kwargs["to_state"] == "degraded"
        assert kwargs["signal"] == "soft"
        assert kwargs["category"] == "http_error"
        assert kwargs["http_status"] == 403
        assert kwargs["resolver"] == "vidara"

    def test_antibot_error_does_not_mutate_or_emit_health(self):
        health = ResolverHealth()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("guest token expired")),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.host_health") as mock_health_diag,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0, health=health)

        assert result is None
        mock_health_diag.assert_not_called()
        assert health.state("vidawra.cc") == HostState.UNKNOWN
        _, kwargs = mock_result.call_args
        assert kwargs["layer"] == "shinden_api"
        assert kwargs["category"] == "anti_bot"

    def test_auto_false_still_attempts_when_host_already_marked_unavailable(self):
        """The single most important contract of this stage: health only observes, never gates."""
        health = ResolverHealth()
        health.record("vidawra.cc", "other-online-id", Signal.HARD)
        assert health.state("vidawra.cc") == HostState.UNAVAILABLE

        stream = MagicMock()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream) as mock_resolve,
            patch("alt_ani_cli.cli.diagnostics.resolve_result"),
            patch("alt_ani_cli.cli.diagnostics.host_health"),
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0, health=health)

        mock_resolve.assert_called_once()
        assert result is stream
        assert embed is _EMBED

    def test_auto_mode_emits_player_selected_before_each_candidate(self):
        player2 = PlayerEntry(online_id="456", player="Backup", lang_audio="pl", lang_subs="pl")
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.cli.diagnostics.resolve_result"),
            patch("alt_ani_cli.cli.diagnostics.player_selected") as mock_player_selected,
        ):
            _resolve_with_fallback(MagicMock(), [_PLAYER, player2], _PLAYER, True, 1.0)

        assert mock_player_selected.call_args_list == [
            call(_PLAYER.online_id, _PLAYER.player, None),
            call(player2.online_id, player2.player, None),
        ]

    def test_auto_false_does_not_emit_player_selected(self):
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")),
            patch("alt_ani_cli.cli.diagnostics.resolve_result"),
            patch("alt_ani_cli.cli.diagnostics.player_selected") as mock_player_selected,
        ):
            _resolve_with_fallback(MagicMock(), [_PLAYER], _PLAYER, False, 1.0)

        mock_player_selected.assert_not_called()
