"""Contract v2: versioning, strict input, limits and the closed error taxonomy."""

import json
from unittest.mock import patch

import pytest

from alt_ani_bridge.contract import LIMITS
from alt_ani_bridge.errors import KINDS, BridgeError, describe
from alt_ani_core import errors as core_errors
from alt_ani_core.transport.api import HttpStatusError, TransportTimeout

from .conftest import SERIES, ep


def raw(client, method: str, text: str) -> dict:
    return json.loads(client.bridge.call(method, text))


class TestBridgeInfo:
    def test_describes_exactly_what_is_callable(self, v2):
        info = json.loads(v2.bridge.call("bridge_info", "{}"))["data"]
        assert info["contract"] == "2.1"
        assert info["capabilities"]["cancel_before_start"] is True
        assert info["capabilities"]["interaction_grants"] is False
        assert info["limits"] == LIMITS
        assert not any("grant" in name or "complete_interaction" in name for name in info["methods"])
        for name in info["methods"]:
            if name in ("create_engine", "close_engine"):
                continue
            # an unknown field is refused before any handler runs, but only by a method that exists
            assert v2.error(name, zz_probe=1)["params"]["problem"] == "unknown_field", name

    def test_other_major_is_refused_and_same_major_accepted(self, v2):
        assert v2.error("preferences_get", contract="3.0")["code"] == "unsupported_contract"
        assert v2.data("preferences_get", contract="2.1")["revision"] == 0


class TestStrictInput:
    @pytest.mark.parametrize("text", ["{not json", "[]", '"x"'])
    def test_unparsable_or_not_an_object(self, v2, text):
        error = raw(v2, "bridge_info", text)["error"]
        assert (error["code"], error["kind"]) == ("bad_request", "invalid_input")

    @pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_non_finite_numbers(self, v2, number):
        text = f'{{"engine_id": "{v2.engine_id}", "series_id": "7", "numbers": [{number}]}}'
        error = raw(v2, "watch_get", text)["error"]
        assert error["params"]["problem"] == "not_finite"

    def test_unknown_field(self, v2):
        error = v2.error("preferences_get", bogus=1)
        assert error["params"] == {"field": "bogus", "problem": "unknown_field"}

    def test_unknown_nested_field(self, v2):
        error = v2.error("preferences_update", changes={"audio": "jp", "colour": "red"}, expected_revision=0)
        assert error["params"]["field"] == "changes.colour"

    def test_bool_is_not_an_integer_and_integer_needs_no_fraction(self, v2):
        assert v2.error("preferences_update", changes={}, expected_revision=True)["params"]["problem"] == "wrong_type"
        assert v2.error("preferences_update", changes={}, expected_revision=1.0)["params"]["problem"] == "wrong_type"

    def test_enum_values(self, v2):
        error = v2.error("network_set", kind="5g")
        assert error["params"]["problem"] == "unknown_value"
        assert error["params"]["expected"] == ["wifi", "cellular", "offline"]

    def test_null_is_not_absence(self, v2):
        assert v2.error("library_list", status=None)["params"]["problem"] == "null"
        assert v2.data("library_list")["items"] == []

    def test_range_checks(self, v2):
        error = v2.error("events_next", cursor={"epoch": "e", "seq": 0})
        assert error["params"] == {"field": "cursor.seq", "problem": "out_of_range", "minimum": 1}


class TestLimits:
    def test_request_size(self, v2):
        text = json.dumps({"engine_id": v2.engine_id, "query": "x" * (LIMITS["request_bytes"] + 1)})
        assert raw(v2, "search", text)["error"]["params"]["problem"] == "too_large"

    def test_string_length(self, v2):
        error = v2.error("search", query="x" * (LIMITS["string_chars"] + 1))
        assert error["params"] == {"field": "query", "problem": "too_large", "limit": LIMITS["string_chars"]}

    @pytest.mark.parametrize(
        ("method", "payload", "limit"),
        [
            ("library_delete", lambda n: {"item_ids": ["i"] * n}, "item_ids"),
            ("queue_reorder", lambda n: {"job_ids": ["j"] * n, "expected_revision": 0}, "job_ids"),
            ("watch_get", lambda n: {"series_id": "7", "numbers": [1] * n}, "episodes"),
            (
                "queue_preview",
                lambda n: {"series": SERIES, "episodes": [ep(1)] * n, "selection": {"mode": "unwatched"}},
                "episodes",
            ),
        ],
    )
    def test_list_limits(self, v2, method, payload, limit):
        error = v2.error(method, **payload(LIMITS[limit] + 1))
        assert (error["params"]["problem"], error["params"]["limit"]) == ("too_large", LIMITS[limit])

    def test_event_batch_and_wait(self, v2):
        cursor = v2.head()
        assert v2.error("events_next", cursor=cursor, max=501)["params"]["problem"] == "too_large"
        assert v2.error("events_next", cursor=cursor, wait_ms=30001)["params"]["problem"] == "too_large"


class TestFailureClassification:
    def test_engine_value_error_is_internal_not_bad_request(self, v2):
        with patch("alt_ani_core.history.queue.SqliteDownloadQueue.get", side_effect=ValueError("https://x/?auth=SECRET")):
            error = v2.error("queue_job", job_id="j")
        assert (error["code"], error["kind"], error["message"]) == ("internal", "internal", "ValueError")
        assert "SECRET" not in json.dumps(error)

    @pytest.mark.parametrize(
        ("exc", "code", "kind"),
        [
            (core_errors.OperationCancelled(), "cancelled", "cancelled"),
            (core_errors.StaleSessionError(), "stale_session", "closed"),
            (BridgeError("bad_request", "x"), "bad_request", "invalid_input"),
            (BridgeError("unknown_engine", "x"), "unknown_engine", "not_found"),
            (BridgeError("runtime.config_conflict", "x"), "runtime.config_conflict", "conflict"),
            (BridgeError("transport_unavailable", "x"), "transport_unavailable", "unavailable"),
            (core_errors.UnknownRecordError("x").coded("state.unknown_job"), "state.unknown_job", "not_found"),
            (core_errors.StateConflictError("x").coded("state.conflict"), "state.conflict", "conflict"),
            (core_errors.InvalidTransitionError("x").coded("state.invalid_transition"), "state.invalid_transition", "conflict"),
            (core_errors.ExecutorBusyError("x").coded("executor.busy"), "executor.busy", "conflict"),
            (
                core_errors.CriteriaMismatchError("x").coded("criteria.no_match", reason="audio"),
                "criteria.no_match",
                "no_match",
            ),
            (
                core_errors.PlaybackBlockedError("x").coded("network.cellular_consent_required"),
                "network.cellular_consent_required",
                "waiting",
            ),
            (core_errors.PlaybackBlockedError("x").coded("network.offline"), "network.offline", "unavailable"),
            (core_errors.InteractionRequiredError("x").coded("interaction.required"), "interaction.required", "waiting"),
            (core_errors.StorageError("x").coded("storage.no_space"), "storage.no_space", "storage"),
            (core_errors.ReloadLimitError("x").coded("episode.reload_limit"), "episode.reload_limit", "limit"),
            (core_errors.HistoryVersionError("x").coded("history.newer_schema"), "history.newer_schema", "internal"),
            (core_errors.AccessBlockedError("x", kind="cloudflare_challenge"), "access_blocked", "access"),
            (HttpStatusError(503), "http_status", "unavailable"),
            (TransportTimeout(), "timeout", "unavailable"),
            (RuntimeError("x"), "internal", "internal"),
        ],
    )
    def test_kind_by_class(self, exc, code, kind):
        described = describe(exc)
        assert (described["code"], described["kind"]) == (code, kind)
        assert described["kind"] in KINDS
