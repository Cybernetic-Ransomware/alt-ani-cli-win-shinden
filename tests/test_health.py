"""Tests for the session-scoped resolver health model and failure classifier."""

import ast
from pathlib import Path

import pytest

from alt_ani_cli import health
from alt_ani_cli.health import (
    EVIDENCE_TTL_SEC,
    HealthTransition,
    HostState,
    ResolverHealth,
    Signal,
    _LayerEffect,
    classify_failure,
)


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _classify(**overrides) -> Signal:
    kwargs = {
        "host": "example.com",
        "layer": "custom",
        "category": None,
        "http_status": None,
        "used_fallback": False,
        "fallback_category": None,
        "fallback_http_status": None,
    }
    kwargs.update(overrides)
    return classify_failure(**kwargs)


@pytest.mark.unit
class TestClassifyFailureIgnore:
    def test_host_none(self):
        assert _classify(host=None, category="timeout") == Signal.IGNORE

    def test_unknown_host(self):
        assert _classify(host="unknown-host", category="timeout") == Signal.IGNORE

    def test_shinden_api_layer(self):
        assert _classify(layer="shinden_api", category="timeout") == Signal.IGNORE

    def test_category_none(self):
        assert _classify(category=None) == Signal.IGNORE


@pytest.mark.unit
class TestClassifyFailureUnsupported:
    def test_unsupported_layer(self):
        assert _classify(layer="unsupported", category="unsupported_host") == Signal.UNSUPPORTED


@pytest.mark.unit
class TestClassifyFailureFile:
    def test_http_404(self):
        assert _classify(category="http_error", http_status=404) == Signal.FILE

    def test_http_410(self):
        assert _classify(category="http_error", http_status=410) == Signal.FILE

    def test_no_stream_url(self):
        assert _classify(category="no_stream_url") == Signal.FILE


@pytest.mark.unit
class TestClassifyFailureSoft:
    def test_parser_drift(self):
        assert _classify(category="parser_drift") == Signal.SOFT

    def test_unknown_category(self):
        assert _classify(category="unknown") == Signal.SOFT

    def test_http_403(self):
        assert _classify(category="http_error", http_status=403) == Signal.SOFT

    def test_http_400(self):
        assert _classify(category="http_error", http_status=400) == Signal.SOFT

    def test_http_error_no_status(self):
        assert _classify(category="http_error", http_status=None) == Signal.SOFT

    def test_unsupported_host_category(self):
        assert _classify(category="unsupported_host") == Signal.SOFT

    def test_http_status_outside_known_ranges(self):
        assert _classify(category="http_error", http_status=200) == Signal.SOFT


@pytest.mark.unit
class TestClassifyFailureTransient:
    def test_http_429(self):
        assert _classify(category="http_error", http_status=429) == Signal.TRANSIENT

    def test_http_5xx(self):
        assert _classify(category="http_error", http_status=503) == Signal.TRANSIENT

    def test_timeout(self):
        assert _classify(category="timeout") == Signal.TRANSIENT

    def test_network_error_single_layer(self):
        assert _classify(category="network_error") == Signal.TRANSIENT


@pytest.mark.unit
class TestClassifyFailureHard:
    def test_network_error_both_layers(self):
        signal = _classify(
            category="network_error",
            used_fallback=True,
            fallback_category="network_error",
        )
        assert signal == Signal.HARD


@pytest.mark.unit
class TestClassifyFailureAggregation:
    def test_timeout_plus_404_is_file(self):
        signal = _classify(
            category="timeout",
            used_fallback=True,
            fallback_category="http_error",
            fallback_http_status=404,
        )
        assert signal == Signal.FILE

    def test_timeout_plus_unsupported_host_is_soft(self):
        signal = _classify(
            category="timeout",
            used_fallback=True,
            fallback_category="unsupported_host",
        )
        assert signal == Signal.SOFT

    def test_never_returns_layer_effect(self):
        for category in ("no_stream_url", "parser_drift", "unknown", "timeout", "network_error"):
            result = _classify(category=category)
            assert isinstance(result, Signal)
            assert not isinstance(result, _LayerEffect)


@pytest.mark.unit
class TestStateTransitions:
    def test_start_unknown(self):
        h = ResolverHealth(clock=_FakeClock())
        assert h.state("host.com") == HostState.UNKNOWN

    def test_single_file_still_unknown(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.FILE)
        assert h.state("host.com") == HostState.UNKNOWN

    def test_soft_is_degraded(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.SOFT)
        assert h.state("host.com") == HostState.DEGRADED

    def test_three_distinct_soft_still_degraded(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.SOFT)
        h.record("host.com", "b", Signal.SOFT)
        h.record("host.com", "c", Signal.SOFT)
        assert h.state("host.com") == HostState.DEGRADED

    def test_single_transient_is_degraded(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.DEGRADED

    def test_two_transient_same_id_still_degraded(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.TRANSIENT)
        h.record("host.com", "a", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.DEGRADED

    def test_transient_two_different_ids_unavailable(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.TRANSIENT)
        h.record("host.com", "b", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.UNAVAILABLE

    def test_hard_is_unavailable(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.HARD)
        assert h.state("host.com") == HostState.UNAVAILABLE

    def test_success_after_unavailable_is_healthy(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.HARD)
        h.record("host.com", "a", Signal.SUCCESS)
        assert h.state("host.com") == HostState.HEALTHY

    def test_file_after_transport_unavailable_clears_transport(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.TRANSIENT)
        h.record("host.com", "b", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.UNAVAILABLE
        h.record("host.com", "c", Signal.FILE)
        assert h.state("host.com") != HostState.UNAVAILABLE

    def test_unsupported_is_unavailable(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.UNSUPPORTED)
        assert h.state("host.com") == HostState.UNAVAILABLE

    def test_unsupported_does_not_expire(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.UNSUPPORTED)
        clock.advance(EVIDENCE_TTL_SEC + 10_000)
        assert h.state("host.com") == HostState.UNAVAILABLE

    def test_should_defer_transport_unavailable(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.HARD)
        assert h.should_defer("host.com") is True

    def test_should_defer_unsupported_false(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.UNSUPPORTED)
        assert h.should_defer("host.com") is False

    def test_should_defer_degraded_false(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.SOFT)
        assert h.should_defer("host.com") is False


@pytest.mark.unit
class TestTtlRegression:
    def test_expired_transport_evidence_does_not_resurrect_unavailable(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.TRANSIENT)
        h.record("host.com", "b", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.UNAVAILABLE

        clock.advance(EVIDENCE_TTL_SEC + 1)
        assert h.state("host.com") == HostState.UNKNOWN

        transition = h.record("host.com", "c", Signal.TRANSIENT)
        assert h.state("host.com") == HostState.DEGRADED

        assert isinstance(transition, HealthTransition)
        assert transition.before == HostState.UNAVAILABLE
        assert transition.after == HostState.DEGRADED
        assert transition.evidence_expired is True


@pytest.mark.unit
class TestHealthTransitionContinuity:
    def test_chained_transitions_before_matches_previous_after(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        transitions = [
            h.record("host.com", "a", Signal.SOFT),
            h.record("host.com", "c", Signal.TRANSIENT),
            h.record("host.com", "d", Signal.TRANSIENT),
            h.record("host.com", "e", Signal.SUCCESS),
        ]

        observed = [t for t in transitions if t is not None]
        assert len(observed) >= 2
        for previous, following in zip(observed, observed[1:], strict=False):
            assert following.before == previous.after


@pytest.mark.unit
class TestTtlBoundaries:
    def test_exactly_at_ttl_still_active(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.HARD)
        clock.advance(EVIDENCE_TTL_SEC)
        assert h.state("host.com") == HostState.UNAVAILABLE

    def test_just_past_ttl_expired(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.HARD)
        clock.advance(EVIDENCE_TTL_SEC + 0.001)
        assert h.state("host.com") == HostState.UNKNOWN

    def test_reverts_to_healthy_after_expiry_when_previously_succeeded(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.SUCCESS)
        h.record("host.com", "a", Signal.HARD)
        clock.advance(EVIDENCE_TTL_SEC + 0.001)
        assert h.state("host.com") == HostState.HEALTHY

    def test_ignore_mutates_nothing(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("host.com", "a", Signal.SOFT)
        before_state = h.state("host.com")
        transition = h.record("host.com", "b", Signal.IGNORE)
        assert transition is None
        assert h.state("host.com") == before_state


@pytest.mark.unit
class TestHostIsolation:
    def test_hard_on_one_host_does_not_affect_another(self):
        clock = _FakeClock()
        h = ResolverHealth(clock=clock)
        h.record("dood.la", "a", Signal.HARD)
        assert h.state("dood.la") == HostState.UNAVAILABLE
        assert h.state("dood.re") == HostState.UNKNOWN


@pytest.mark.unit
class TestModulePurity:
    def test_no_forbidden_imports(self):
        source = Path(health.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"diagnostics", "extract", "cli", "ui", "shinden", "curl_cffi", "yt_dlp"}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                for part in node.module.split("."):
                    imported.add(part)
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
        assert imported.isdisjoint(forbidden), imported & forbidden


@pytest.mark.unit
class TestCategoryLiteralsMatchExtractCommon:
    def test_known_category_constants_are_classified(self):
        from alt_ani_cli.extract import common as extract_common

        assert classify_failure(
            host="example.com",
            layer="custom",
            category=extract_common.CATEGORY_NO_STREAM_URL,
            http_status=None,
            used_fallback=False,
            fallback_category=None,
            fallback_http_status=None,
        ) == Signal.FILE
        assert classify_failure(
            host="example.com",
            layer="custom",
            category=extract_common.CATEGORY_PARSER_DRIFT,
            http_status=None,
            used_fallback=False,
            fallback_category=None,
            fallback_http_status=None,
        ) == Signal.SOFT
