from unittest.mock import MagicMock, call

import pytest
from alt_ani_core.config import ExtractOptions
from alt_ani_core.errors import AntiBotError, NoStreamError, ShindenXhrError, SourcesExhaustedError
from alt_ani_core.events import (
    EmbedResolved,
    ExtractorFallback,
    HostDeferred,
    HostDropped,
    HostLastResort,
    PhaseFinished,
    PhaseStarted,
    SourceAttemptFailed,
)
from alt_ani_core.models import EmbedURL, ExtractionFailure, FallbackPolicy, PlayerEntry, ResolvedStream
from alt_ani_core.rules.health import HostState, Signal
from alt_ani_core.testing import make_stream

SINGLE = FallbackPolicy.SINGLE
WALK = FallbackPolicy.WALK

_PLAYER = PlayerEntry(online_id="123", player="Default", lang_audio="pl", lang_subs="pl")
_BACKUP = PlayerEntry(online_id="456", player="Backup", lang_audio="pl", lang_subs="pl")
_EMBED = EmbedURL(url="https://vidawra.cc/e/abc123", referer="https://shinden.pl/")
_PLAYER_A = PlayerEntry(online_id="a1", player="PlayerA", lang_audio="pl", lang_subs="pl")
_PLAYER_B = PlayerEntry(online_id="b1", player="PlayerB", lang_audio="pl", lang_subs="pl")
_EMBED_HOST_A = EmbedURL(url="https://hosta.example/e/a1", referer="https://shinden.pl/")
_EMBED_HOST_B = EmbedURL(url="https://hostb.example/e/b1", referer="https://shinden.pl/")
_EMBED_X_A = EmbedURL(url="https://hostx.example/e/a1", referer="https://shinden.pl/")
_EMBED_X_B = EmbedURL(url="https://hostx.example/e/b1", referer="https://shinden.pl/")


def _no_stream(message: str = "all extractors failed", **failure) -> NoStreamError:
    err = NoStreamError(message)
    if failure:
        err.failure = ExtractionFailure(**failure)
    return err


def _embeds(mapping: dict[str, EmbedURL]):
    def _side_effect(_client, online_id, **_kwargs):
        return mapping[online_id]

    return _side_effect


def _extract_by_url(outcomes: dict[str, object]):
    def _side_effect(url, _referer, **_kwargs):
        outcome = outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _side_effect


def _resolved_ids(embed_seam) -> list[str]:
    return [c.args[1] for c in embed_seam.call_args_list]


def _prime_cache(session, embed_seam, player: PlayerEntry, embed: EmbedURL) -> None:
    embed_seam.return_value = embed
    assert session.probe_hosts([player]) is None
    embed_seam.reset_mock(return_value=True, side_effect=True)


def _xhr(**fields) -> ShindenXhrError:
    defaults = dict(stage="player_load", category="http_error", reason="http_status")
    return ShindenXhrError("xhr failed", **(defaults | fields))


@pytest.fixture
def session(opened, embed_seam, extract_seam, diag):
    return opened()[1]


@pytest.fixture
def engine(make_engine, session):
    return session._engine


@pytest.mark.unit
class TestResolveDiagnostics:
    def test_success_returns_stream_source_and_embed_and_records_ok_result(self, session, embed_seam, extract_seam, diag):
        stream = make_stream()
        embed_seam.return_value = _EMBED
        extract_seam.return_value = stream

        resolved = session.resolve(_PLAYER)

        assert resolved == ResolvedStream(stream=stream, source=_PLAYER, embed=_EMBED)
        assert resolved.stream is stream and resolved.embed is _EMBED
        diag.resolve_result.assert_called_once()
        args, kwargs = diag.resolve_result.call_args
        assert args[0] == "vidawra.cc"
        assert args[1] is True
        assert args[2] is None
        assert kwargs == {"xhr_backend": "new_xhr", "token_refreshed": False, "xhr_fallback": None}

    def test_seams_get_the_session_state_and_extract_options(self, session, engine, embed_seam, extract_seam):
        embed_seam.return_value = _EMBED
        options = ExtractOptions(cookies_file="cookies.txt")

        session.resolve(_PLAYER, options=options)

        assert embed_seam.call_args.args[1] == _PLAYER.online_id
        assert embed_seam.call_args.kwargs["min_sleep_seconds"] == engine.config.antibot_delay_sec
        assert embed_seam.call_args.kwargs["xhr"].episode_url == session.episode.url
        assert extract_seam.call_args.args == (_EMBED.url, _EMBED.referer)
        assert extract_seam.call_args.kwargs["options"] is options

    def test_extractor_failure_forwards_layer_category_status_and_fallback(self, session, embed_seam, extract_seam, diag):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = _no_stream(
            layer="jwplayer",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="timeout",
            fallback_http_status=None,
        )

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER)

        diag.resolve_result.assert_called_once()
        args, kwargs = diag.resolve_result.call_args
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
        assert kwargs["xhr_backend"] == "new_xhr"

    def test_untagged_exception_forwards_none_fields(self, session, embed_seam, extract_seam, diag):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = NoStreamError("boom")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        _, kwargs = diag.resolve_result.call_args
        assert kwargs["layer"] is None
        assert kwargs["category"] is None
        assert kwargs["http_status"] is None
        assert kwargs["used_fallback"] is None
        assert kwargs["fallback_layer"] is None
        assert kwargs["fallback_category"] is None

    def test_antibot_error_from_embed_resolution_tags_shinden_layer(self, session, embed_seam, extract_seam, diag):
        embed_seam.side_effect = AntiBotError("guest token expired")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        args, kwargs = diag.resolve_result.call_args
        assert args[0] is None
        assert args[2] == "AntiBotError"
        assert kwargs["layer"] == "shinden_api"
        assert kwargs["category"] == "anti_bot"
        extract_seam.assert_not_called()


@pytest.mark.unit
class TestSourcesExhausted:
    def test_single_lists_the_one_attempt(self, session, embed_seam, extract_seam):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = NoStreamError("boom")

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER, candidates=[_PLAYER, _BACKUP])

        err = exc_info.value
        assert [(a.source, a.host, a.error) for a in err.attempts] == [(_PLAYER, "vidawra.cc", "NoStreamError")]
        assert err.code == "episode.sources_exhausted"
        assert err.params == {"number": 1.0}
        assert _resolved_ids(embed_seam) == [_PLAYER.online_id]

    def test_walk_lists_every_attempt_in_order_with_unknown_host_for_embed_failures(self, session, embed_seam, extract_seam):
        embed_seam.side_effect = [AntiBotError("blocked"), _EMBED]
        extract_seam.side_effect = NoStreamError("boom")

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_BACKUP, candidates=[_PLAYER, _BACKUP], policy=WALK)

        assert [(a.source, a.host, a.error) for a in exc_info.value.attempts] == [
            (_BACKUP, None, "AntiBotError"),
            (_PLAYER, "vidawra.cc", "NoStreamError"),
        ]

    def test_walk_starts_with_the_chosen_source_and_tries_it_once(self, session, embed_seam, extract_seam):
        embed_seam.side_effect = AntiBotError("blocked")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER_B, candidates=[_PLAYER, _PLAYER_B, _BACKUP], policy=WALK)

        assert _resolved_ids(embed_seam) == ["b1", "123", "456"]

    def test_walk_stops_at_the_first_source_that_works(self, session, embed_seam, extract_seam):
        embed_seam.side_effect = _embeds({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B, "123": _EMBED})
        stream_b = make_stream("https://cdn.example/b.mp4")
        extract_seam.side_effect = _extract_by_url({_EMBED_HOST_A.url: NoStreamError("a failed"), _EMBED_HOST_B.url: stream_b})

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B, _PLAYER], policy=WALK)

        assert (resolved.stream, resolved.source, resolved.embed) == (stream_b, _PLAYER_B, _EMBED_HOST_B)
        assert _resolved_ids(embed_seam) == ["a1", "b1"]


@pytest.mark.unit
class TestHealthRecording:
    def test_success_records_health_and_emits_host_health(self, session, engine, embed_seam, diag):
        embed_seam.return_value = _EMBED

        session.resolve(_PLAYER)

        assert engine.health.state("vidawra.cc") == HostState.HEALTHY
        diag.resolve_result.assert_called_once()
        diag.host_health.assert_called_once()
        _, kwargs = diag.host_health.call_args
        assert kwargs["host"] == "vidawra.cc"
        assert kwargs["from_state"] == "unknown"
        assert kwargs["to_state"] == "healthy"
        assert kwargs["signal"] == "success"
        assert kwargs["online_id"] == _PLAYER.online_id
        assert kwargs["category"] is None
        assert kwargs["http_status"] is None
        assert kwargs["resolver"] == "vidara"
        assert kwargs["evidence_expired"] is False

    def test_no_stream_error_classifies_and_emits_host_health(self, session, engine, embed_seam, extract_seam, diag):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = _no_stream(layer="jwplayer", category="http_error", http_status=403)

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        assert engine.health.state("vidawra.cc") == HostState.DEGRADED
        diag.resolve_result.assert_called_once()
        diag.host_health.assert_called_once()
        _, kwargs = diag.host_health.call_args
        assert kwargs["to_state"] == "degraded"
        assert kwargs["signal"] == "soft"
        assert kwargs["category"] == "http_error"
        assert kwargs["http_status"] == 403
        assert kwargs["resolver"] == "vidara"

    def test_antibot_error_does_not_mutate_or_emit_health(self, session, engine, embed_seam, diag):
        embed_seam.side_effect = AntiBotError("guest token expired")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        diag.host_health.assert_not_called()
        assert engine.health.state("vidawra.cc") == HostState.UNKNOWN
        _, kwargs = diag.resolve_result.call_args
        assert kwargs["layer"] == "shinden_api"
        assert kwargs["category"] == "anti_bot"

    def test_single_still_attempts_when_host_already_marked_unavailable(self, session, engine, embed_seam, extract_seam, diag):
        """A person's pick is only observed by health, never gated."""
        engine.health.record("vidawra.cc", "other-online-id", Signal.HARD)
        assert engine.health.state("vidawra.cc") == HostState.UNAVAILABLE
        embed_seam.return_value = _EMBED

        resolved = session.resolve(_PLAYER)

        extract_seam.assert_called_once()
        assert resolved.stream is extract_seam.return_value
        assert resolved.embed is _EMBED
        diag.health_defer.assert_not_called()

    def test_walk_emits_player_selected_before_each_candidate(self, session, embed_seam, diag):
        embed_seam.side_effect = AntiBotError("blocked")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER, candidates=[_PLAYER, _BACKUP], policy=WALK)

        assert diag.player_selected.call_args_list == [
            call(_PLAYER.online_id, _PLAYER.player, None),
            call(_BACKUP.online_id, _BACKUP.player, None),
        ]

    def test_single_does_not_emit_player_selected(self, session, embed_seam, diag):
        embed_seam.side_effect = AntiBotError("blocked")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        diag.player_selected.assert_not_called()


@pytest.mark.unit
class TestDeferAndLastResort:
    """WALK defers a candidate whose host is already UNAVAILABLE; the last-resort pass retries it once."""

    def test_deferred_host_skips_extraction_while_alternative_succeeds(
        self, session, engine, embed_seam, extract_seam, diag, sink
    ):
        engine.health.record("hosta.example", "seed", Signal.HARD)
        assert engine.health.should_defer("hosta.example") is True
        stream_b = make_stream("https://cdn.example/b.mp4")
        embed_seam.side_effect = _embeds({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B})
        extract_seam.side_effect = _extract_by_url({_EMBED_HOST_B.url: stream_b})

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert resolved.stream is stream_b
        assert resolved.embed is _EMBED_HOST_B
        assert resolved.source is _PLAYER_B
        # host A's embed is resolved; only its extraction is skipped
        assert _resolved_ids(embed_seam) == ["a1", "b1"]
        extract_seam.assert_called_once()
        assert extract_seam.call_args.args[0] == _EMBED_HOST_B.url
        diag.health_defer.assert_called_once()
        _, kwargs = diag.health_defer.call_args
        assert kwargs["action"] == "deferred"
        assert kwargs["host"] == "hosta.example"
        assert kwargs["online_id"] == "a1"
        assert kwargs["player"] == "PlayerA"
        assert kwargs["state"] == "unavailable"
        assert [c.args[0] for c in diag.resolve_result.call_args_list] == ["hostb.example"]
        assert sink.of(HostDeferred) == [HostDeferred(host="hosta.example")]
        assert sink.of(HostLastResort) == []

    def test_last_resort_retries_deferred_candidate_on_the_same_embed(
        self, session, engine, embed_seam, extract_seam, diag, sink
    ):
        engine.health.record("hosta.example", "seed", Signal.HARD)
        stream_a = make_stream("https://cdn.example/a.mp4")
        exc_b = _no_stream("b failed", layer="custom", category="http_error", http_status=403)
        embed_seam.side_effect = _embeds({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B})
        extract_seam.side_effect = _extract_by_url({_EMBED_HOST_B.url: exc_b, _EMBED_HOST_A.url: stream_a})

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert resolved.stream is stream_a
        assert resolved.embed is _EMBED_HOST_A
        assert resolved.source is _PLAYER_A
        assert _resolved_ids(embed_seam).count("a1") == 1
        assert [c.kwargs["action"] for c in diag.health_defer.call_args_list] == ["deferred", "retry"]
        assert [c.args[0] for c in diag.player_selected.call_args_list] == ["a1", "b1", "a1"]
        assert [c.args[0] for c in diag.resolve_result.call_args_list] == ["hostb.example", "hosta.example"]
        assert sink.of(HostLastResort) == [HostLastResort(host="hosta.example")]

    def test_second_deferred_candidate_dropped_when_host_still_unavailable_after_retry(
        self, session, engine, embed_seam, extract_seam, diag, sink
    ):
        engine.health.record("hostx.example", "seed", Signal.HARD)
        embed_seam.side_effect = _embeds({"a1": _EMBED_X_A, "b1": _EMBED_X_B})
        extract_seam.side_effect = _no_stream(
            "still down", layer="custom", category="network_error", used_fallback=True, fallback_category="network_error"
        )

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert [c.kwargs["action"] for c in diag.health_defer.call_args_list] == ["deferred", "deferred", "retry", "dropped"]
        # only the real retry on A; B was dropped, not extracted
        assert diag.resolve_result.call_count == 1
        extract_seam.assert_called_once()
        assert [a.source for a in exc_info.value.attempts] == [_PLAYER_A]
        assert sink.of(HostDropped) == [HostDropped(host="hostx.example")]

    def test_second_deferred_candidate_attempted_normally_once_host_recovers(
        self, session, engine, embed_seam, extract_seam, diag, sink
    ):
        engine.health.record("hostx.example", "seed", Signal.HARD)
        stream_b = make_stream("https://cdn.example/b.mp4")
        exc_a = _no_stream("gone", layer="custom", category="http_error", http_status=404)
        embed_seam.side_effect = _embeds({"a1": _EMBED_X_A, "b1": _EMBED_X_B})
        extract_seam.side_effect = _extract_by_url({_EMBED_X_A.url: exc_a, _EMBED_X_B.url: stream_b})

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert resolved.stream is stream_b
        assert resolved.embed is _EMBED_X_B
        assert [c.kwargs["action"] for c in diag.health_defer.call_args_list] == ["deferred", "deferred", "retry", "retry"]
        assert diag.resolve_result.call_count == 2
        assert sink.of(HostDropped) == []

    def test_single_never_defers_even_when_host_unavailable(self, session, engine, embed_seam, extract_seam, diag, sink):
        engine.health.record("hosta.example", "seed", Signal.HARD)
        embed_seam.return_value = _EMBED_HOST_A

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B])

        extract_seam.assert_called_once()
        diag.health_defer.assert_not_called()
        assert sink.of(HostDeferred) == []
        assert resolved.stream is extract_seam.return_value

    def test_walk_without_health_evidence_never_defers(self, session, embed_seam, extract_seam, diag):
        embed_seam.return_value = _EMBED_HOST_A

        resolved = session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        extract_seam.assert_called_once()
        diag.health_defer.assert_not_called()
        assert resolved.source is _PLAYER_A

    def test_unsupported_host_is_never_deferred(self, session, engine, embed_seam, extract_seam, diag):
        engine.health.record("unsupported.example", "seed", Signal.UNSUPPORTED)
        assert engine.health.should_defer("unsupported.example") is False
        embed_seam.return_value = EmbedURL(url="https://unsupported.example/e/a1", referer="https://shinden.pl/")
        extract_seam.side_effect = _no_stream("unsupported host", layer="unsupported", category="unsupported_host")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER_A, candidates=[_PLAYER_A], policy=WALK)

        extract_seam.assert_called_once()
        diag.health_defer.assert_not_called()
        diag.resolve_result.assert_called_once()


@pytest.fixture
def learn_label(engine) -> MagicMock:
    engine.health.learn_label = MagicMock(wraps=engine.health.learn_label)
    return engine.health.learn_label


@pytest.mark.unit
class TestLearnLabel:
    """learn_label runs on every real host discovery: before should_defer, never in the last-resort pass."""

    def test_called_with_host_before_extraction(self, session, embed_seam, extract_seam, learn_label):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = lambda *_a, **_k: (learn_label.assert_called_once(), make_stream())[1]

        session.resolve(_PLAYER)

        learn_label.assert_called_once_with(_PLAYER.player, "vidawra.cc")

    def test_called_on_cache_hit(self, session, embed_seam, learn_label):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)

        session.resolve(_PLAYER)

        embed_seam.assert_not_called()
        learn_label.assert_called_once_with(_PLAYER.player, "vidawra.cc")

    def test_called_before_should_defer_for_deferred_candidate(self, session, engine, embed_seam, extract_seam, learn_label):
        engine.health.record("hosta.example", "seed", Signal.HARD)
        embed_seam.side_effect = _embeds({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B})

        session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert call(_PLAYER_A.player, "hosta.example") in learn_label.call_args_list

    def test_not_called_again_in_last_resort_retry(self, session, engine, embed_seam, extract_seam, learn_label):
        engine.health.record("hosta.example", "seed", Signal.HARD)
        exc_b = _no_stream("b failed", layer="custom", category="http_error", http_status=403)
        embed_seam.side_effect = _embeds({"a1": _EMBED_HOST_A, "b1": _EMBED_HOST_B})
        extract_seam.side_effect = _extract_by_url({_EMBED_HOST_B.url: exc_b, _EMBED_HOST_A.url: make_stream()})

        session.resolve(_PLAYER_A, candidates=[_PLAYER_A, _PLAYER_B], policy=WALK)

        assert learn_label.call_args_list == [
            call(_PLAYER_A.player, "hosta.example"),
            call(_PLAYER_B.player, "hostb.example"),
        ]

    def test_stale_cache_refresh_relearns_new_host_when_it_differs(self, session, embed_seam, extract_seam, learn_label):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)
        embed_seam.return_value = EmbedURL(url="https://otherhost.example/e/123", referer="https://shinden.pl/")
        extract_seam.side_effect = [NoStreamError("stale token"), make_stream()]

        session.resolve(_PLAYER)

        assert learn_label.call_args_list == [
            call(_PLAYER.player, "vidawra.cc"),
            call(_PLAYER.player, "otherhost.example"),
        ]


@pytest.mark.unit
class TestEmbedCache:
    def test_cache_hit_skips_the_xhr_and_the_antibot_phase(self, session, embed_seam, extract_seam, sink):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)
        sink.events.clear()

        resolved = session.resolve(_PLAYER)

        embed_seam.assert_not_called()
        assert resolved.embed is _EMBED
        assert extract_seam.call_args.args[0] == _EMBED.url
        assert sink.of(PhaseStarted) == [] and sink.of(PhaseFinished) == []

    def test_fresh_resolve_is_wrapped_in_the_antibot_phase_and_cached(self, session, embed_seam, sink):
        embed_seam.return_value = _EMBED

        session.resolve(_PLAYER)
        session.resolve(_PLAYER)

        embed_seam.assert_called_once()
        assert sink.of(PhaseStarted) == [PhaseStarted(phase="antibot_delay")]
        assert sink.of(PhaseFinished) == [PhaseFinished(phase="antibot_delay")]
        assert session.known_sources[_PLAYER.online_id].embed_url == _EMBED.url

    def test_stale_cached_embed_gets_exactly_one_fresh_resolve(self, session, embed_seam, extract_seam, diag):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)
        fresh = EmbedURL(url="https://vidawra.cc/e/fresh", referer="https://shinden.pl/")
        stream = make_stream()
        embed_seam.return_value = fresh
        extract_seam.side_effect = [NoStreamError("stale token"), stream]

        resolved = session.resolve(_PLAYER)

        embed_seam.assert_called_once()
        assert [c.args[0] for c in extract_seam.call_args_list] == [_EMBED.url, fresh.url]
        assert (resolved.stream, resolved.embed) == (stream, fresh)
        assert session.known_sources[_PLAYER.online_id].embed_url == fresh.url
        # the stale attempt is not a failure of its own: one result for the source
        assert [c.args[1] for c in diag.resolve_result.call_args_list] == [True]

    def test_fresh_embed_failing_too_is_not_resolved_a_second_time(self, session, embed_seam, extract_seam, diag):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)
        embed_seam.return_value = EmbedURL(url="https://vidawra.cc/e/fresh", referer="https://shinden.pl/")
        extract_seam.side_effect = NoStreamError("dead")

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER)

        embed_seam.assert_called_once()
        assert extract_seam.call_count == 2
        assert len(exc_info.value.attempts) == 1
        assert [c.args[1] for c in diag.resolve_result.call_args_list] == [False]

    def test_failure_on_a_freshly_resolved_embed_is_not_retried(self, session, embed_seam, extract_seam):
        embed_seam.return_value = _EMBED
        extract_seam.side_effect = NoStreamError("dead")

        with pytest.raises(SourcesExhaustedError):
            session.resolve(_PLAYER)

        embed_seam.assert_called_once()
        extract_seam.assert_called_once()


@pytest.mark.unit
class TestRedaction:
    def test_events_redact_query_values_while_the_extractor_gets_the_real_url(self, session, embed_seam, extract_seam, sink):
        embed_seam.return_value = EmbedURL(url="https://vidawra.cc/e/abc123?t=EMBED_SECRET", referer="https://shinden.pl/")
        extract_seam.side_effect = NoStreamError("all extractors failed: https://cdn.example/master.m3u8?token=SECRET")

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER)

        assert sink.of(EmbedResolved) == [EmbedResolved(redacted_url="https://vidawra.cc/e/abc123?t=<redacted>")]
        failed = sink.of(SourceAttemptFailed)
        assert len(failed) == 1
        assert (failed[0].player, failed[0].episode) == ("Default", 1.0)
        assert "cdn.example/master.m3u8?token=<redacted>" in failed[0].redacted_text
        assert "SECRET" not in repr(sink.events)
        assert "SECRET" not in repr(exc_info.value) + str(exc_info.value) + repr(exc_info.value.attempts)
        assert extract_seam.call_args.args[0] == "https://vidawra.cc/e/abc123?t=EMBED_SECRET"

    def test_extractor_fallback_notice_is_forwarded_as_an_event(self, session, embed_seam, extract_seam, sink):
        embed_seam.return_value = _EMBED

        def _extract(_url, _referer, *, on_fallback, **_kwargs):
            on_fallback("extractor_fallback", "vidawra.cc", "HTTP 403")
            return make_stream()

        extract_seam.side_effect = _extract

        session.resolve(_PLAYER)

        assert sink.of(ExtractorFallback) == [
            ExtractorFallback(kind="extractor_fallback", host="vidawra.cc", redacted_text="HTTP 403")
        ]


@pytest.mark.unit
class TestXhrPolicy:
    @pytest.mark.parametrize("policy", [SINGLE, WALK])
    @pytest.mark.parametrize(
        "err",
        [
            _xhr(http_status=429),
            _xhr(http_status=200, api_code="vpnp_limit"),
            _xhr(http_status=200, api_code="invalid_token"),
            _xhr(http_status=403, api_code="invalid_token"),
            _xhr(http_status=200, api_code="brak_playera"),
            _xhr(http_status=503),
            _xhr(http_status=500),
            _xhr(category="timeout", reason="transport"),
            _xhr(category="parser_drift", reason="wait_above_limit", http_status=200),
        ],
        ids=[
            "429",
            "vpnp_limit",
            "invalid_token-200",
            "invalid_token-403",
            "unknown-code-200",
            "503",
            "500",
            "timeout",
            "invalid-wait",
        ],
    )
    def test_shared_api_failure_is_reraised_after_one_xhr(
        self, session, engine, embed_seam, extract_seam, diag, sink, err, policy
    ):
        embed_seam.side_effect = err

        with pytest.raises(ShindenXhrError) as exc_info:
            session.resolve(_PLAYER, candidates=[_PLAYER, _BACKUP], policy=policy)

        assert exc_info.value is err
        embed_seam.assert_called_once()
        extract_seam.assert_not_called()
        diag.host_health.assert_not_called()
        diag.resolve_result.assert_called_once()
        assert diag.resolve_result.call_args.args[:3] == (None, False, "ShindenXhrError")
        assert sink.of(SourceAttemptFailed) == []

    @pytest.mark.parametrize(
        "err",
        [
            _xhr(http_status=404),
            _xhr(http_status=410, api_code="brak_playera"),
            _xhr(stage="player_show", category="no_stream_url", reason="no_iframe", http_status=200),
        ],
        ids=["404", "410-with-code", "no-iframe"],
    )
    def test_player_local_failure_moves_to_next_player(self, session, embed_seam, extract_seam, sink, err):
        stream = make_stream()
        embed_seam.side_effect = [err, _EMBED]
        extract_seam.return_value = stream

        resolved = session.resolve(_PLAYER, candidates=[_PLAYER, _BACKUP], policy=WALK)

        assert (resolved.stream, resolved.embed, resolved.source) == (stream, _EMBED, _BACKUP)
        assert _resolved_ids(embed_seam) == [_PLAYER.online_id, _BACKUP.online_id]
        assert [e.player for e in sink.of(SourceAttemptFailed)] == [_PLAYER.player]

    def test_player_local_failure_on_a_single_source_exhausts_it(self, session, embed_seam):
        embed_seam.side_effect = _xhr(http_status=404)

        with pytest.raises(SourcesExhaustedError) as exc_info:
            session.resolve(_PLAYER, candidates=[_PLAYER, _BACKUP])

        assert [(a.source, a.host, a.error) for a in exc_info.value.attempts] == [(_PLAYER, None, "ShindenXhrError")]

    def test_failure_forwards_xhr_diagnostic_fields(self, session, embed_seam, diag):
        embed_seam.side_effect = _xhr(
            stage="player_show", http_status=429, api_code="turnstile", retry_after=30, reason="api_error"
        )

        with pytest.raises(ShindenXhrError):
            session.resolve(_PLAYER)

        args, kwargs = diag.resolve_result.call_args
        assert args[2] == "ShindenXhrError"
        assert {k: kwargs[k] for k in ("layer", "category", "http_status", "stage", "api_code", "retry_after", "reason")} == {
            "layer": "shinden_api",
            "category": "http_error",
            "http_status": 429,
            "stage": "player_show",
            "api_code": "turnstile",
            "retry_after": 30,
            "reason": "api_error",
        }
        assert kwargs["xhr_outcome"] == "access_blocked"
        assert kwargs["xhr_backend"] == "new_xhr"
        assert kwargs["token_refreshed"] is False
        assert kwargs["xhr_fallback"] is None

    def test_xhr_failure_never_feeds_host_health(self, session, engine, embed_seam, extract_seam, diag):
        _prime_cache(session, embed_seam, _PLAYER, _EMBED)
        embed_seam.side_effect = _xhr(http_status=503)
        extract_seam.side_effect = NoStreamError("expired")

        with pytest.raises(ShindenXhrError):
            session.resolve(_PLAYER)

        assert engine.health.state("vidawra.cc") is HostState.UNKNOWN
        diag.host_health.assert_not_called()
