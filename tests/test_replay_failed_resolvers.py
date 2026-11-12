"""Tests for tools/replay_failed_resolvers.py — log parser, classification, and mocked replay."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_core.errors import AntiBotError, NoStreamError, ShindenXhrError
from alt_ani_core.models import ExtractionFailure
from alt_ani_core.shinden.xhr import BACKEND_API4, EpisodeXhr
from alt_ani_core.transport.api import HttpStatusError, TransportTimeout

# tests/ has no top-level __init__.py, so pytest puts tests/ (not the repo root) on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.replay_failed_resolvers import (  # noqa: E402
    LogParseError,
    ReplayCase,
    Verdict,
    _NowSnapshot,
    classify_outcome,
    main,
    parse_diagnostics_log,
    replay_case,
)

_PREFIX = "2026-09-25 21:03:11,482"
_TRANSPORT = MagicMock(name="transport")
_EP_PATH = "/episode/1-fate/view/11"


@pytest.fixture(autouse=True)
def _episode_page():
    """The replayed episode page lists the logged players; individual tests override it when the page matters."""
    xhr = EpisodeXhr("https://shinden.pl" + _EP_PATH, frozenset({"42", "a", "b", "c"}), None, BACKEND_API4)
    with patch("tools.replay_failed_resolvers.shinden_xhr.load_episode", return_value=([], xhr)) as mock_load:
        yield mock_load


def _line(event: str, **fields: object) -> str:
    """Mirror diagnostics._emit's format: quote whitespace-containing values, drop None fields."""
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if " " in text:
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    return f"{_PREFIX} " + " ".join(parts)


def _case(**overrides: object) -> ReplayCase:
    base = dict(
        series_id="s1",
        series_title="Series",
        episode_number=1.0,
        episode_title="Ep",
        online_id="42",
        player_name="CDA",
        host_before=None,
        exc_before=None,
        layer_before=None,
        category_before=None,
        http_status_before=None,
        used_fallback_before=None,
        fallback_layer_before=None,
        fallback_category_before=None,
        fallback_http_status_before=None,
        episode_path=_EP_PATH,
    )
    base.update(overrides)
    return ReplayCase(**base)


def _no_stream(**failure: object) -> NoStreamError:
    exc = NoStreamError("still failing")
    exc.failure = ExtractionFailure(**failure)
    return exc


def _now(**overrides: object) -> _NowSnapshot:
    base = dict(ok=False, host=None, harness_error=False)
    base.update(overrides)
    return _NowSnapshot(**base)


@pytest.mark.unit
class TestParseDiagnosticsLog:
    def test_builds_case_with_series_episode_player_context(self, tmp_path):
        lines = [
            _line("series_selected", id="123", title="Attack on Titan"),
            _line("episode_selected", number=5, title="Ep Five"),
            _line("player_selected", online_id="99", player="CDA", host="cda.pl"),
            _line(
                "resolve_result",
                host="cda.pl",
                ok=False,
                exc="NoStreamError",
                layer="ytdlp",
                category="http_error",
                http_status=403,
                used_fallback=False,
                elapsed=1.234,
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert len(cases) == 1
        case = cases[0]
        assert case.series_id == "123"
        assert case.series_title == "Attack on Titan"
        assert case.episode_number == 5.0
        assert case.episode_title == "Ep Five"
        assert case.online_id == "99"
        assert case.player_name == "CDA"
        assert case.host_before == "cda.pl"
        assert case.layer_before == "ytdlp"
        assert case.category_before == "http_error"
        assert case.http_status_before == 403
        assert case.used_fallback_before is False

    def test_ignores_successful_resolve_result(self, tmp_path):
        lines = [
            _line("player_selected", online_id="1", player="X"),
            _line("resolve_result", host="x.com", ok=True, elapsed=0.5),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert cases == []
        assert skipped == 0

    def test_online_id_none_when_no_preceding_player_selected(self, tmp_path):
        lines = [_line("resolve_result", host="x.com", ok=False, exc="NoStreamError", elapsed=0.1)]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, _ = parse_diagnostics_log(log)

        assert cases[0].online_id is None
        assert cases[0].player_name is None

    def test_skips_malformed_lines_and_keeps_going(self, tmp_path):
        lines = [
            "this is not a diagnostics line",
            _line("player_selected", online_id="1", player="X"),
            _line("resolve_result", host="x.com", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 1
        assert len(cases) == 1

    def test_log_parse_error_when_nothing_recognized(self, tmp_path):
        log = tmp_path / "session.log"
        log.write_text("garbage\nmore garbage\n", encoding="utf-8")

        with pytest.raises(LogParseError):
            parse_diagnostics_log(log)

    def test_player_context_does_not_leak_across_episodes_without_player_selected(self, tmp_path):
        lines = [
            _line("episode_selected", number=1, title="Ep One"),
            _line("player_selected", online_id="111", player="CDA"),
            _line("resolve_result", host="cda.pl", ok=False, exc="NoStreamError", elapsed=0.1),
            _line("episode_selected", number=2, title="Ep Two"),
            _line("resolve_result", host="cda.pl", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, _ = parse_diagnostics_log(log)

        assert len(cases) == 2
        assert cases[0].online_id == "111"
        assert cases[1].online_id is None

    def test_player_context_reset_by_series_selected(self, tmp_path):
        lines = [
            _line("player_selected", online_id="111", player="CDA"),
            _line("series_selected", id="s2", title="Other Series"),
            _line("resolve_result", host="cda.pl", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, _ = parse_diagnostics_log(log)

        assert cases[0].online_id is None

    def test_new_health_events_and_mode_are_parsed_without_creating_replay_cases(self, tmp_path):
        lines = [
            _line("session_start", version="1.0.0", python="3.14.0", platform="Windows", mode="interactive"),
            _line("series_selected", id="123", title="Attack on Titan"),
            _line("episode_selected", number=5, title="Ep Five"),
            _line("player_selected", online_id="99", player="CDA", host="cda.pl"),
            _line(
                "host_health",
                host="cda.pl",
                from_state="healthy",
                to_state="degraded",
                signal="soft",
                online_id="99",
                category="parser_drift",
                resolver="ytdlp",
                evidence_expired=False,
            ),
            _line(
                "health_defer",
                action="deferred",
                host="cda.pl",
                online_id="99",
                player="CDA",
                state="degraded",
                category="parser_drift",
            ),
            _line(
                "resolve_result",
                host="cda.pl",
                ok=False,
                exc="NoStreamError",
                layer="ytdlp",
                category="http_error",
                http_status=403,
                used_fallback=False,
                elapsed=1.234,
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert len(cases) == 1
        case = cases[0]
        assert case.series_id == "123"
        assert case.episode_number == 5.0
        assert case.online_id == "99"
        assert case.player_name == "CDA"
        assert case.host_before == "cda.pl"

    def test_noninteractive_run_with_two_failing_players_yields_two_cases(self, tmp_path):
        lines = [
            _line("session_start", version="1.0.0", python="3.14.0", platform="Windows", mode="noninteractive"),
            _line("series_selected", id="123", title="Attack on Titan"),
            _line("episode_selected", number=5, title="Ep Five"),
            _line("player_selected", online_id="99", player="CDA"),
            _line(
                "host_health",
                host="cda.pl",
                from_state="unknown",
                to_state="degraded",
                signal="soft",
                online_id="99",
                category="parser_drift",
                resolver="ytdlp",
                evidence_expired=False,
            ),
            _line(
                "resolve_result",
                host="cda.pl",
                ok=False,
                exc="NoStreamError",
                layer="ytdlp",
                category="parser_drift",
                used_fallback=False,
                elapsed=1.1,
            ),
            _line("player_selected", online_id="88", player="Vidara"),
            _line(
                "host_health",
                host="vidawra.cc",
                from_state="unknown",
                to_state="degraded",
                signal="soft",
                online_id="88",
                category="http_error",
                http_status=403,
                resolver="vidara",
                evidence_expired=False,
            ),
            _line(
                "resolve_result",
                host="vidawra.cc",
                ok=False,
                exc="NoStreamError",
                layer="custom",
                category="http_error",
                http_status=403,
                used_fallback=False,
                elapsed=0.9,
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert len(cases) == 2
        assert cases[0].online_id == "99"
        assert cases[0].player_name == "CDA"
        assert cases[0].host_before == "cda.pl"
        assert cases[1].online_id == "88"
        assert cases[1].player_name == "Vidara"
        assert cases[1].host_before == "vidawra.cc"

    def test_deferred_and_dropped_health_defer_events_never_create_cases(self, tmp_path):
        """deferred/dropped never extract, so they must never surface as replayable cases — only real attempts do."""
        lines = [
            _line("player_selected", online_id="a1", player="APlayer"),
            _line(
                "health_defer", action="deferred", host="hostx.example", online_id="a1", player="APlayer", state="unavailable"
            ),
            _line("player_selected", online_id="b1", player="BPlayer"),
            _line(
                "resolve_result",
                host="hosty.example",
                ok=False,
                exc="NoStreamError",
                layer="custom",
                category="http_error",
                http_status=403,
                elapsed=0.5,
            ),
            _line("player_selected", online_id="a1", player="APlayer"),
            _line("health_defer", action="retry", host="hostx.example", online_id="a1", player="APlayer", state="unavailable"),
            _line(
                "resolve_result",
                host="hostx.example",
                ok=False,
                exc="NoStreamError",
                layer="custom",
                category="network_error",
                elapsed=0.3,
            ),
            _line(
                "health_defer", action="dropped", host="hostx.example", online_id="c1", player="CPlayer", state="unavailable"
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert len(cases) == 2
        assert cases[0].online_id == "b1"
        assert cases[0].host_before == "hosty.example"
        assert cases[1].online_id == "a1"
        assert cases[1].host_before == "hostx.example"

    def test_health_reorder_is_parsed_without_creating_a_case_or_disturbing_context(self, tmp_path):
        """health_reorder never carries an online_id/host of its own — replay must ignore its payload."""
        lines = [
            _line("episode_selected", number=5, title="Ep Five"),
            _line(
                "health_reorder",
                number=5,
                before="a1,b1",
                after="b1,a1",
                states="healthy,degraded",
            ),
            _line("player_selected", online_id="b1", player="BPlayer"),
            _line(
                "resolve_result",
                host="hostb.example",
                ok=False,
                exc="NoStreamError",
                layer="custom",
                category="http_error",
                http_status=403,
                elapsed=0.5,
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert len(cases) == 1
        assert cases[0].online_id == "b1"
        assert cases[0].player_name == "BPlayer"
        assert cases[0].episode_number == 5.0
        assert cases[0].host_before == "hostb.example"

    def test_quoted_values_with_spaces_round_trip(self, tmp_path):
        lines = [
            _line("episode_selected", number=1, title="A Title With Spaces"),
            _line("player_selected", online_id="1", player="Multi Word Player"),
            _line("resolve_result", host="x.com", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines), encoding="utf-8")

        cases, _ = parse_diagnostics_log(log)

        assert cases[0].episode_title == "A Title With Spaces"
        assert cases[0].player_name == "Multi Word Player"


@pytest.mark.unit
class TestClassifyOutcome:
    def test_fixed_when_now_ok_and_host_unchanged(self):
        case = _case(host_before="cda.pl")
        now = _now(ok=True, host="cda.pl")
        assert classify_outcome(case, now) is Verdict.FIXED

    def test_replay_error_takes_priority_over_everything(self):
        case = _case(host_before="cda.pl")
        now = _now(ok=True, host="cda.pl", harness_error=True)
        assert classify_outcome(case, now) is Verdict.REPLAY_ERROR

    def test_source_rotated_takes_priority_over_fixed(self):
        case = _case(host_before="cda.pl")
        now = _now(ok=True, host="vidara.to")
        assert classify_outcome(case, now) is Verdict.SOURCE_ROTATED

    def test_source_rotated_takes_priority_over_same_failure(self):
        case = _case(host_before="cda.pl", layer_before="ytdlp", category_before="http_error", http_status_before=403)
        now = _now(ok=False, host="vidara.to", layer="ytdlp", category="http_error", http_status=403)
        assert classify_outcome(case, now) is Verdict.SOURCE_ROTATED

    def test_no_source_rotation_when_host_before_unknown(self):
        case = _case(host_before=None, layer_before="shinden_api", category_before="anti_bot")
        now = _now(ok=False, host="cda.pl", layer="shinden_api", category="anti_bot")
        assert classify_outcome(case, now) is Verdict.SAME_FAILURE

    def test_same_failure_when_full_signature_identical_including_fallback(self):
        case = _case(
            layer_before="custom",
            category_before="http_error",
            http_status_before=403,
            used_fallback_before=True,
            fallback_layer_before="ytdlp",
            fallback_category_before="network_error",
            fallback_http_status_before=None,
        )
        now = _now(
            ok=False,
            layer="custom",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="network_error",
            fallback_http_status=None,
        )
        assert classify_outcome(case, now) is Verdict.SAME_FAILURE

    def test_diagnostics_improved_when_only_fallback_gets_more_specific(self):
        """Primary layer/category/http_status unchanged; fallback goes unknown -> http_error/403."""
        case = _case(
            layer_before="custom",
            category_before="http_error",
            http_status_before=403,
            used_fallback_before=True,
            fallback_layer_before="ytdlp",
            fallback_category_before="unknown",
            fallback_http_status_before=None,
        )
        now = _now(
            ok=False,
            layer="custom",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="http_error",
            fallback_http_status=403,
        )
        assert classify_outcome(case, now) is Verdict.DIAGNOSTICS_IMPROVED

    def test_changed_failure_when_a_concrete_field_flips(self):
        case = _case(layer_before="jwplayer", category_before="http_error", http_status_before=403)
        now = _now(ok=False, layer="jwplayer", category="network_error", http_status=None)
        assert classify_outcome(case, now) is Verdict.CHANGED_FAILURE

    def test_changed_failure_when_layer_itself_changes(self):
        case = _case(layer_before="jwplayer", category_before="http_error", http_status_before=403)
        now = _now(ok=False, layer="ytdlp", category="http_error", http_status=403)
        assert classify_outcome(case, now) is Verdict.CHANGED_FAILURE

    def test_none_to_unknown_category_is_not_changed_failure(self):
        case = _case(layer_before="jwplayer", category_before=None, http_status_before=403)
        now = _now(ok=False, layer="jwplayer", category="unknown", http_status=403)
        assert classify_outcome(case, now) is Verdict.SAME_FAILURE

    def test_unknown_to_none_category_is_not_changed_failure(self):
        case = _case(layer_before="jwplayer", category_before="unknown", http_status_before=403)
        now = _now(ok=False, layer="jwplayer", category=None, http_status=403)
        assert classify_outcome(case, now) is Verdict.SAME_FAILURE


@pytest.mark.unit
class TestReplayCase:
    def test_success_is_fixed(self):
        case = _case(host_before="cda.pl")
        client = MagicMock()
        embed = MagicMock(url="https://cda.pl/video/abc", referer="https://shinden.pl/")
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", return_value=MagicMock()) as mock_extract,
        ):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        mock_extract.assert_called_once_with(embed.url, embed.referer, transport=_TRANSPORT)
        assert outcome.verdict is Verdict.FIXED
        assert outcome.now_ok is True
        assert outcome.now_host == "cda.pl"

    def test_antibot_error_classified_as_shinden_api_anti_bot(self):
        case = _case(host_before="cda.pl", layer_before="ytdlp", category_before="http_error", http_status_before=403)
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        assert outcome.now_layer == "shinden_api"
        assert outcome.now_category == "anti_bot"
        assert outcome.now_exc == "AntiBotError"
        assert outcome.now_host is None

    def test_no_stream_error_pulls_annotated_attrs_and_matches_same_failure(self):
        case = _case(
            host_before="vidara.to",
            layer_before="custom",
            category_before="http_error",
            http_status_before=403,
            used_fallback_before=True,
            fallback_layer_before="ytdlp",
            fallback_category_before="network_error",
            fallback_http_status_before=None,
        )
        client = MagicMock()
        embed = MagicMock(url="https://vidara.to/e/abc", referer="https://shinden.pl/")
        exc = _no_stream(
            layer="custom",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="network_error",
            fallback_http_status=None,
        )
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=exc),
        ):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        assert outcome.now_layer == "custom"
        assert outcome.now_fallback_category == "network_error"
        assert outcome.verdict is Verdict.SAME_FAILURE

    def test_no_stream_error_with_more_specific_fallback_is_diagnostics_improved(self):
        case = _case(
            host_before="vidara.to",
            layer_before="custom",
            category_before="http_error",
            http_status_before=403,
            used_fallback_before=True,
            fallback_layer_before="ytdlp",
            fallback_category_before="unknown",
            fallback_http_status_before=None,
        )
        client = MagicMock()
        embed = MagicMock(url="https://vidara.to/e/abc", referer="https://shinden.pl/")
        exc = _no_stream(
            layer="custom",
            category="http_error",
            http_status=403,
            used_fallback=True,
            fallback_layer="ytdlp",
            fallback_category="http_error",
            fallback_http_status=403,
        )
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=exc),
        ):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        assert outcome.verdict is Verdict.DIAGNOSTICS_IMPROVED

    @pytest.mark.parametrize(
        ("exc", "category", "status"),
        [
            (TransportTimeout(), "timeout", None),
            (HttpStatusError(503, url="https://shinden.pl/x?auth=SECRET"), "http_error", 503),
        ],
        ids=["timeout", "http-503"],
    )
    def test_transport_error_during_embed_is_classified_not_a_replay_error(self, exc, category, status):
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=exc):
            outcome = replay_case(MagicMock(), _case(), transport=_TRANSPORT)

        assert outcome.verdict is not Verdict.REPLAY_ERROR
        assert (outcome.now_layer, outcome.now_category, outcome.now_http_status) == ("shinden_api", category, status)
        assert outcome.now_exc == type(exc).__name__

    def test_unexpected_error_during_embed_is_replay_error(self):
        case = _case()
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=RuntimeError("boom")):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        assert outcome.verdict is Verdict.REPLAY_ERROR

    def test_unexpected_error_during_extract_is_replay_error(self):
        case = _case()
        client = MagicMock()
        embed = MagicMock(url="https://cda.pl/video/abc", referer="https://shinden.pl/")
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=RuntimeError("boom")),
        ):
            outcome = replay_case(client, case, transport=_TRANSPORT)

        assert outcome.verdict is Verdict.REPLAY_ERROR

    def test_missing_online_id_is_replay_error_without_network_call(self):
        case = _case(online_id=None)
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed") as mock_resolve_embed:
            outcome = replay_case(client, case, transport=_TRANSPORT)

        mock_resolve_embed.assert_not_called()
        assert outcome.verdict is Verdict.REPLAY_ERROR


def _xhr(**fields) -> ShindenXhrError:
    defaults = dict(stage="player_load", category="http_error", reason="http_status")
    return ShindenXhrError("xhr failed", **(defaults | fields))


@pytest.mark.unit
class TestReplayXhrErrors:
    def test_resolve_result_with_xhr_fields_parses_without_skips(self, tmp_path):
        lines = [
            _line("series_selected", id="1", title="Fate"),
            _line("episode_selected", number=1.0, title="Ep 1"),
            _line("player_selected", online_id="42", player="Cda"),
            _line(
                "resolve_result",
                ok=False,
                exc="ShindenXhrError",
                layer="shinden_api",
                category="http_error",
                http_status=429,
                stage="player_load",
                api_code="turnstile",
                retry_after=30,
                reason="api_error",
                xhr_outcome="access_blocked",
                elapsed=0.1,
            ),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cases, skipped = parse_diagnostics_log(log)

        assert skipped == 0
        assert [(c.online_id, c.layer_before, c.http_status_before) for c in cases] == [("42", "shinden_api", 429)]

    def test_xhr_failure_takes_layer_and_category_from_the_error(self):
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=_xhr(http_status=404)):
            outcome = replay_case(MagicMock(), _case(), transport=_TRANSPORT)
        assert (outcome.now_layer, outcome.now_category, outcome.now_http_status) == ("shinden_api", "http_error", 404)
        assert outcome.xhr_stop is None

    @pytest.mark.parametrize("err", [_xhr(http_status=429), _xhr(http_status=503)], ids=["429", "503"])
    def test_shared_api_failure_marks_outcome_as_stop(self, err):
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=err):
            outcome = replay_case(MagicMock(), _case(), transport=_TRANSPORT)
        assert outcome.xhr_stop is not None

    def test_main_stops_replaying_after_a_block(self, tmp_path):
        lines = [
            _line("series_selected", id="1", title="Fate"),
            _line("episode_selected", number=1.0, title="Ep 1", path=_EP_PATH),
        ]
        for oid in ("a", "b", "c"):
            lines += [
                _line("player_selected", online_id=oid, player="Cda"),
                _line("resolve_result", ok=False, exc="NoStreamError", elapsed=0.1),
            ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (
            patch("tools.replay_failed_resolvers.CurlTransport"),
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=_xhr(http_status=429)) as mock_resolve,
            patch("tools.replay_failed_resolvers.progress"),
        ):
            assert main([str(log)]) == 0
        mock_resolve.assert_called_once()


@pytest.mark.unit
class TestReplayEpisodeContext:
    def test_episode_path_and_token_refreshed_are_parsed(self, tmp_path):
        lines = [
            _line("series_selected", id="1", title="Fate"),
            _line("episode_selected", number=1.0, title="Ep 1", path=_EP_PATH),
            _line("player_selected", online_id="42", player="Cda"),
            _line("resolve_result", ok=False, exc="ShindenXhrError", xhr_backend="new_xhr", token_refreshed=True, elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cases, skipped = parse_diagnostics_log(log)
        assert skipped == 0
        assert [c.episode_path for c in cases] == [_EP_PATH]

    def test_old_log_without_episode_path_is_a_replay_error_without_requests(self, _episode_page, tmp_path):
        lines = [
            _line("series_selected", id="1", title="Fate"),
            _line("episode_selected", number=1.0, title="Ep 1"),
            _line("player_selected", online_id="42", player="Cda"),
            _line("resolve_result", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (case,), _ = parse_diagnostics_log(log)
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed") as mock_resolve:
            outcome = replay_case(MagicMock(), case, transport=_TRANSPORT)
        assert outcome.verdict is Verdict.REPLAY_ERROR
        assert outcome.note == "missing episode context in log"
        _episode_page.assert_not_called()
        mock_resolve.assert_not_called()

    @pytest.mark.parametrize("path", ["//evil.example/episode/1-x/view/2", "/xhr/42/player_load", "/episode/1-x/view/2?auth=x"])
    def test_untrusted_logged_path_is_never_fetched(self, _episode_page, path):
        outcome = replay_case(MagicMock(), _case(episode_path=path), transport=_TRANSPORT)
        assert outcome.note == "missing episode context in log"
        _episode_page.assert_not_called()

    def test_page_is_rebuilt_from_the_logged_path_without_pagination(self, _episode_page):
        client = MagicMock()
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=AntiBotError("x")),
            patch("alt_ani_core.shinden.series.list_episodes") as mock_list,
        ):
            replay_case(client, _case(), backend="new_xhr", transport=_TRANSPORT)
        _episode_page.assert_called_once_with(client, "https://shinden.pl" + _EP_PATH, backend="new_xhr")
        mock_list.assert_not_called()

    def test_player_now_the_honeypot_gets_no_request(self, _episode_page):
        _episode_page.return_value[1].trap_id = "42"
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed") as mock_resolve:
            outcome = replay_case(MagicMock(), _case(), transport=_TRANSPORT)
        assert outcome.verdict is Verdict.REPLAY_ERROR
        assert "honeypot" in (outcome.note or "")
        mock_resolve.assert_not_called()

    def test_player_no_longer_listed_gets_no_request(self, _episode_page):
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed") as mock_resolve:
            outcome = replay_case(MagicMock(), _case(online_id="999"), transport=_TRANSPORT)
        assert outcome.verdict is Verdict.REPLAY_ERROR
        assert "no longer listed" in (outcome.note or "")
        mock_resolve.assert_not_called()

    def test_outcome_reports_the_backend_and_any_fallback(self, _episode_page):
        _episode_page.return_value[1].fallback_reason = "auth_missing"
        embed = MagicMock(url="https://cda.pl/video/abc", referer="https://shinden.pl/")
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", return_value=MagicMock()),
        ):
            outcome = replay_case(MagicMock(), _case(host_before="cda.pl"), transport=_TRANSPORT)
        assert outcome.now_backend == BACKEND_API4
        assert outcome.note == "api4 fallback: auth_missing"


@pytest.mark.unit
class TestReplayBackendChoice:
    def _main(self, tmp_path, argv_extra=()):
        lines = [
            _line("series_selected", id="1", title="Fate"),
            _line("episode_selected", number=1.0, title="Ep 1", path=_EP_PATH),
            _line("player_selected", online_id="42", player="Cda"),
            _line("resolve_result", ok=False, exc="NoStreamError", elapsed=0.1),
        ]
        log = tmp_path / "session.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (
            patch("tools.replay_failed_resolvers.CurlTransport"),
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=AntiBotError("x")),
            patch("tools.replay_failed_resolvers.progress"),
        ):
            return main([str(log), *argv_extra])

    @pytest.mark.parametrize(
        ("env", "flag", "expected"),
        [
            ("new", None, "new_xhr"),
            ("api4", None, "api4"),
            ("api4", "new", "new_xhr"),
            ("new", "api4", "api4"),
        ],
    )
    def test_flag_wins_over_env(self, monkeypatch, tmp_path, _episode_page, env, flag, expected):
        monkeypatch.setenv("ALT_ANI_CLI_XHR_BACKEND", env)
        assert self._main(tmp_path, ("--xhr-backend", flag) if flag else ()) == 0
        assert _episode_page.call_args.kwargs["backend"] == expected

    def test_invalid_env_value_fails_before_any_request(self, monkeypatch, tmp_path, _episode_page):
        monkeypatch.setenv("ALT_ANI_CLI_XHR_BACKEND", "legacy")
        assert self._main(tmp_path) == 1
        _episode_page.assert_not_called()
