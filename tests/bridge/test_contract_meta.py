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


_RETRY_LATER = "Automatic attempts stopped — you can retry later by picking a player manually."
_STOPPED = "Automatic attempts stopped."
_RELOAD = "Automatic attempts stopped — reload the episode to see the current players."
_NEXT = "— you can try another player."
_HTTP_STATUS = {"category": "http_error", "reason": "http_status"}
_API_ERROR = {"category": "http_error", "reason": "api_error"}
_LOAD, _SHOW, _REFRESH = "player_load", "player_show", "token_refresh"
_CHANGED = {"stage": _REFRESH, "category": "player_list_changed"}

_XHR_MATRIX = [
    pytest.param(
        {"stage": _LOAD, "http_status": 403, **_HTTP_STATUS},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on player_load (HTTP 403). {_RETRY_LATER}",
        id="403",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 401, **_HTTP_STATUS},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on player_load (HTTP 401). {_RETRY_LATER}",
        id="401",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 429, "retry_after": 7, **_HTTP_STATUS},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on player_load (HTTP 429). {_RETRY_LATER} The server asks to wait 7 s.",
        id="429_retry_after",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 503, **_HTTP_STATUS},
        "xhr_service_unavailable",
        True,
        f"shinden player API is unavailable on player_load (HTTP 503). {_RETRY_LATER}",
        id="503",
    ),
    pytest.param(
        {"stage": _SHOW, "http_status": 503, "api_code": "ochrona_godz", **_API_ERROR},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on player_show (HTTP 503, code=ochrona_godz). {_RETRY_LATER}",
        id="503_block_code",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 200, "api_code": "invalid_token", **_API_ERROR},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on player_load (HTTP 200, code=invalid_token). {_RETRY_LATER}",
        id="200_invalid_token",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 500, "api_code": "brak_playera", **_API_ERROR},
        "xhr_service_unavailable",
        True,
        f"shinden player API is unavailable on player_load (HTTP 500, code=brak_playera). {_RETRY_LATER}",
        id="500_unknown_code",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 400, "api_code": "brak_playera", **_API_ERROR},
        "xhr_api_protocol_error",
        False,
        f"shinden player API returned an unexpected response on player_load (HTTP 400, code=brak_playera). {_STOPPED}",
        id="400_unknown_code",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 404, "api_code": "brak_playera", **_API_ERROR},
        "xhr_next_player",
        False,
        f"This player did not work on player_load (HTTP 404, code=brak_playera) {_NEXT}",
        id="404_unknown_code",
    ),
    pytest.param(
        {"stage": _SHOW, "category": "timeout", "reason": "transport"},
        "xhr_service_unavailable",
        True,
        f"shinden player API is unavailable on player_show (timeout). {_RETRY_LATER}",
        id="timeout",
    ),
    pytest.param(
        {"stage": _SHOW, "category": "network_error", "reason": "transport"},
        "xhr_service_unavailable",
        True,
        f"shinden player API is unavailable on player_show (network_error). {_RETRY_LATER}",
        id="network_error",
    ),
    pytest.param(
        {**_CHANGED, "reason": "player_not_listed"},
        "xhr_players_changed",
        False,
        f"The player list of this episode changed after the token refresh (reason=player_not_listed). {_RELOAD}",
        id="player_not_listed",
    ),
    pytest.param(
        {**_CHANGED, "reason": "player_is_honeypot"},
        "xhr_players_changed",
        False,
        f"The player list of this episode changed after the token refresh (reason=player_is_honeypot). {_RELOAD}",
        id="player_is_honeypot",
    ),
    pytest.param(
        {"stage": _REFRESH, "http_status": 404, **_HTTP_STATUS},
        "xhr_api_protocol_error",
        False,
        f"shinden player API returned an unexpected response on token_refresh (HTTP 404). {_STOPPED}",
        id="refresh_404",
    ),
    pytest.param(
        {"stage": _REFRESH, "http_status": 503, **_HTTP_STATUS},
        "xhr_service_unavailable",
        True,
        f"shinden player API is unavailable on token_refresh (HTTP 503). {_RETRY_LATER}",
        id="refresh_503",
    ),
    pytest.param(
        {**_CHANGED, "reason": "player_not_listed", "http_status": 403},
        "xhr_access_blocked",
        False,
        f"shinden blocked access to its player API on token_refresh (reason=player_not_listed). {_RETRY_LATER}",
        id="refresh_403_players_changed",
    ),
    pytest.param(
        {"stage": _SHOW, "category": "no_stream_url", "reason": "no_iframe", "http_status": 200},
        "xhr_next_player",
        False,
        f"This player did not work on player_show (reason=no_iframe) {_NEXT}",
        id="no_iframe",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 404, **_HTTP_STATUS},
        "xhr_next_player",
        False,
        f"This player did not work on player_load (HTTP 404) {_NEXT}",
        id="404",
    ),
    pytest.param(
        {"stage": _LOAD, "http_status": 410, **_HTTP_STATUS},
        "xhr_next_player",
        False,
        f"This player did not work on player_load (HTTP 410) {_NEXT}",
        id="410",
    ),
    pytest.param(
        {"stage": _LOAD, "category": "parser_drift", "reason": "wait_above_limit", "http_status": 200},
        "xhr_api_protocol_error",
        False,
        f"shinden player API returned an unexpected response on player_load (reason=wait_above_limit). {_STOPPED}",
        id="wait_above_limit",
    ),
    pytest.param(
        {"stage": _LOAD, "category": "xhr_context_invalid", "reason": "context_missing"},
        "xhr_api_protocol_error",
        False,
        f"shinden player API returned an unexpected response on player_load (reason=context_missing). {_STOPPED}",
        id="context_missing",
    ),
]


class TestXhrErrorDescription:
    """Literal expectations, never derived from xhr_outcome, so any change to the rule surfaces as a contract change."""

    @pytest.mark.parametrize(("fields", "code", "retryable", "message"), _XHR_MATRIX)
    def test_full_description(self, fields, code, retryable, message):
        described = describe(core_errors.ShindenXhrError("raw server SECRET", **fields))
        assert described == {
            "code": code,
            "kind": "unavailable",
            "retryable": retryable,
            "message": message,
            "params": {
                "stage": fields["stage"],
                "api_code": fields.get("api_code"),
                "http_status": fields.get("http_status"),
                "retry_after": fields.get("retry_after"),
                "reason": fields["reason"],
            },
        }
        assert "SECRET" not in json.dumps(described)

    def test_matrix_covers_every_outcome(self):
        assert {case.values[1] for case in _XHR_MATRIX} == {
            "xhr_access_blocked",
            "xhr_service_unavailable",
            "xhr_api_protocol_error",
            "xhr_players_changed",
            "xhr_next_player",
        }

    def test_unexpected_exception_reports_only_its_type(self):
        assert describe(RuntimeError("https://x/?auth=token")) == {
            "code": "internal",
            "kind": "internal",
            "retryable": False,
            "message": "RuntimeError",
            "params": {},
        }
