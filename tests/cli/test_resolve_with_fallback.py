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


_PLAYER_A = PlayerEntry(online_id="a1", player="PlayerA", lang_audio="pl", lang_subs="pl")
_PLAYER_B = PlayerEntry(online_id="b1", player="PlayerB", lang_audio="pl", lang_subs="pl")
_EMBED_HOST_A = EmbedURL(url="https://hosta.example/e/a1", referer="https://shinden.pl/")
_EMBED_HOST_B = EmbedURL(url="https://hostb.example/e/b1", referer="https://shinden.pl/")


def _resolve_embed_from(embeds: dict[str, EmbedURL], calls: list[str] | None = None):
    def _side_effect(_client, online_id):
        if calls is not None:
            calls.append(online_id)
        return embeds[online_id]

    return _side_effect


def _extract_by_url(outcomes: dict[str, object]):
    """outcomes maps embed url -> Stream (success) or Exception instance (failure)."""

    def _side_effect(url, _referer, **_kwargs):
        outcome = outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _side_effect


@pytest.mark.unit
class TestResolveWithFallbackDeferAndLastResort:
    """auto=True defers a candidate whose host is already UNAVAILABLE; last-resort retries it once."""

    def test_deferred_host_skips_extraction_while_alternative_succeeds(self):
        health = ResolverHealth()
        health.record("hosta.example", "seed", Signal.HARD)
        assert health.should_defer("hosta.example") is True

        stream_b = MagicMock()
        resolve_embed_calls: list[str] = []
        with (
            patch(
                "alt_ani_cli.cli.shinden_api.resolve_embed",
                side_effect=_resolve_embed_from({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B}, resolve_embed_calls),
            ),
            patch(
                "alt_ani_cli.cli.extract.resolve",
                side_effect=_extract_by_url({_EMBED_HOST_B.url: stream_b}),
            ) as mock_extract,
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A, _PLAYER_B], _PLAYER_A, True, 1.0, health=health)

        assert result is stream_b
        assert embed is _EMBED_HOST_B
        assert resolve_embed_calls == ["a1", "b1"]  # host A's embed IS resolved — only extraction is skipped
        mock_extract.assert_called_once()
        assert mock_extract.call_args.args[0] == _EMBED_HOST_B.url
        mock_defer.assert_called_once()
        _, kwargs = mock_defer.call_args
        assert kwargs["action"] == "deferred"
        assert kwargs["host"] == "hosta.example"
        assert kwargs["online_id"] == "a1"
        hosts_recorded = [c.args[0] for c in mock_result.call_args_list]
        assert hosts_recorded == ["hostb.example"]  # no resolve_result for the deferred host

    def test_last_resort_retries_deferred_candidate_on_the_same_embed(self):
        health = ResolverHealth()
        health.record("hosta.example", "seed", Signal.HARD)

        stream_a = MagicMock()
        exc_b = NoStreamError("b failed")
        for name, value in dict(
            layer="custom",
            category="http_error",
            http_status=403,
            used_fallback=False,
            fallback_category=None,
            fallback_http_status=None,
        ).items():
            setattr(exc_b, name, value)

        resolve_embed_calls: list[str] = []
        with (
            patch(
                "alt_ani_cli.cli.shinden_api.resolve_embed",
                side_effect=_resolve_embed_from({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B}, resolve_embed_calls),
            ),
            patch(
                "alt_ani_cli.cli.extract.resolve",
                side_effect=_extract_by_url({_EMBED_HOST_B.url: exc_b, _EMBED_HOST_A.url: stream_a}),
            ),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
            patch("alt_ani_cli.cli.diagnostics.player_selected") as mock_player_selected,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A, _PLAYER_B], _PLAYER_A, True, 1.0, health=health)

        assert result is stream_a
        assert embed is _EMBED_HOST_A
        assert resolve_embed_calls.count("a1") == 1  # no second resolve_embed for the last-resort retry
        actions = [c.kwargs["action"] for c in mock_defer.call_args_list]
        assert actions == ["deferred", "retry"]
        player_ids_selected = [c.args[0] for c in mock_player_selected.call_args_list]
        assert player_ids_selected == ["a1", "b1", "a1"]  # A re-selected before its last-resort retry
        hosts_recorded = [c.args[0] for c in mock_result.call_args_list]
        assert hosts_recorded == ["hostb.example", "hosta.example"]

    def test_second_deferred_candidate_dropped_when_host_still_unavailable_after_retry(self):
        embed_a = EmbedURL(url="https://hostx.example/e/a1", referer="https://shinden.pl/")
        embed_b = EmbedURL(url="https://hostx.example/e/b1", referer="https://shinden.pl/")
        health = ResolverHealth()
        health.record("hostx.example", "seed", Signal.HARD)

        exc = NoStreamError("still down")
        for name, value in dict(
            layer="custom",
            category="network_error",
            http_status=None,
            used_fallback=True,
            fallback_category="network_error",
            fallback_http_status=None,
        ).items():
            setattr(exc, name, value)

        with (
            patch(
                "alt_ani_cli.cli.shinden_api.resolve_embed",
                side_effect=_resolve_embed_from({"a1": embed_a, "b1": embed_b}),
            ),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=exc),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A, _PLAYER_B], _PLAYER_A, True, 1.0, health=health)

        assert result is None
        actions = [c.kwargs["action"] for c in mock_defer.call_args_list]
        assert actions == ["deferred", "deferred", "retry", "dropped"]
        assert mock_result.call_count == 1  # only the real retry attempt on A — B was dropped, not extracted

    def test_second_deferred_candidate_attempted_normally_once_host_recovers(self):
        embed_a = EmbedURL(url="https://hostx.example/e/a1", referer="https://shinden.pl/")
        embed_b = EmbedURL(url="https://hostx.example/e/b1", referer="https://shinden.pl/")
        health = ResolverHealth()
        health.record("hostx.example", "seed", Signal.HARD)

        exc_a = NoStreamError("gone")
        for name, value in dict(
            layer="custom",
            category="http_error",
            http_status=404,
            used_fallback=False,
            fallback_category=None,
            fallback_http_status=None,
        ).items():
            setattr(exc_a, name, value)
        stream_b = MagicMock()

        with (
            patch(
                "alt_ani_cli.cli.shinden_api.resolve_embed",
                side_effect=_resolve_embed_from({"a1": embed_a, "b1": embed_b}),
            ),
            patch(
                "alt_ani_cli.cli.extract.resolve",
                side_effect=_extract_by_url({embed_a.url: exc_a, embed_b.url: stream_b}),
            ),
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A, _PLAYER_B], _PLAYER_A, True, 1.0, health=health)

        assert result is stream_b
        assert embed is embed_b
        actions = [c.kwargs["action"] for c in mock_defer.call_args_list]
        assert actions == ["deferred", "deferred", "retry", "retry"]  # B not dropped — host recovered after A's retry
        assert mock_result.call_count == 2

    def test_auto_false_never_defers_even_when_host_unavailable(self):
        health = ResolverHealth()
        health.record("hosta.example", "seed", Signal.HARD)

        stream = MagicMock()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED_HOST_A),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream) as mock_extract,
            patch("alt_ani_cli.cli.diagnostics.resolve_result"),
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A], _PLAYER_A, False, 1.0, health=health)

        mock_extract.assert_called_once()
        mock_defer.assert_not_called()
        assert result is stream

    def test_health_none_never_defers(self):
        stream = MagicMock()
        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=_EMBED_HOST_A),
            patch("alt_ani_cli.cli.extract.resolve", return_value=stream) as mock_extract,
            patch("alt_ani_cli.cli.diagnostics.resolve_result"),
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A, _PLAYER_B], _PLAYER_A, True, 1.0, health=None)

        mock_extract.assert_called_once()
        mock_defer.assert_not_called()
        assert result is stream

    def test_unsupported_host_is_never_deferred(self):
        health = ResolverHealth()
        health.record("unsupported.example", "seed", Signal.UNSUPPORTED)
        assert health.should_defer("unsupported.example") is False

        embed = EmbedURL(url="https://unsupported.example/e/a1", referer="https://shinden.pl/")
        exc = NoStreamError("unsupported host")
        for name, value in dict(
            layer="unsupported",
            category="unsupported_host",
            http_status=None,
            used_fallback=False,
            fallback_category=None,
            fallback_http_status=None,
        ).items():
            setattr(exc, name, value)

        with (
            patch("alt_ani_cli.cli.shinden_api.resolve_embed", return_value=embed),
            patch("alt_ani_cli.cli.extract.resolve", side_effect=exc) as mock_extract,
            patch("alt_ani_cli.cli.diagnostics.resolve_result") as mock_result,
            patch("alt_ani_cli.cli.diagnostics.health_defer") as mock_defer,
        ):
            result, _embed = _resolve_with_fallback(MagicMock(), [_PLAYER_A], _PLAYER_A, True, 1.0, health=health)

        assert result is None
        mock_extract.assert_called_once()
        mock_defer.assert_not_called()
        mock_result.assert_called_once()
