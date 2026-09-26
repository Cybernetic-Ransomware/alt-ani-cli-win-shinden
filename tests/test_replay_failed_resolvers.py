"""Tests for tools/replay_failed_resolvers.py — log parser, classification, and mocked replay."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alt_ani_cli.errors import AntiBotError, NoStreamError

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
    parse_diagnostics_log,
    replay_case,
)

_PREFIX = "2026-09-25 21:03:11,482"


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
    )
    base.update(overrides)
    return ReplayCase(**base)


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
            patch("tools.replay_failed_resolvers.extract.resolve", return_value=MagicMock()),
        ):
            outcome = replay_case(client, case)

        assert outcome.verdict is Verdict.FIXED
        assert outcome.now_ok is True
        assert outcome.now_host == "cda.pl"

    def test_antibot_error_classified_as_shinden_api_anti_bot(self):
        case = _case(host_before="cda.pl", layer_before="ytdlp", category_before="http_error", http_status_before=403)
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=AntiBotError("blocked")):
            outcome = replay_case(client, case)

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
        exc = NoStreamError("still failing")
        exc.layer = "custom"
        exc.category = "http_error"
        exc.http_status = 403
        exc.used_fallback = True
        exc.fallback_layer = "ytdlp"
        exc.fallback_category = "network_error"
        exc.fallback_http_status = None
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=exc),
        ):
            outcome = replay_case(client, case)

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
        exc = NoStreamError("still failing")
        exc.layer = "custom"
        exc.category = "http_error"
        exc.http_status = 403
        exc.used_fallback = True
        exc.fallback_layer = "ytdlp"
        exc.fallback_category = "http_error"
        exc.fallback_http_status = 403
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=exc),
        ):
            outcome = replay_case(client, case)

        assert outcome.verdict is Verdict.DIAGNOSTICS_IMPROVED

    def test_unexpected_error_during_embed_is_replay_error(self):
        case = _case()
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", side_effect=RuntimeError("boom")):
            outcome = replay_case(client, case)

        assert outcome.verdict is Verdict.REPLAY_ERROR

    def test_unexpected_error_during_extract_is_replay_error(self):
        case = _case()
        client = MagicMock()
        embed = MagicMock(url="https://cda.pl/video/abc", referer="https://shinden.pl/")
        with (
            patch("tools.replay_failed_resolvers.shinden_api.resolve_embed", return_value=embed),
            patch("tools.replay_failed_resolvers.extract.resolve", side_effect=RuntimeError("boom")),
        ):
            outcome = replay_case(client, case)

        assert outcome.verdict is Verdict.REPLAY_ERROR

    def test_missing_online_id_is_replay_error_without_network_call(self):
        case = _case(online_id=None)
        client = MagicMock()
        with patch("tools.replay_failed_resolvers.shinden_api.resolve_embed") as mock_resolve_embed:
            outcome = replay_case(client, case)

        mock_resolve_embed.assert_not_called()
        assert outcome.verdict is Verdict.REPLAY_ERROR
