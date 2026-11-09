"""resolve_embed: player_load wait contract, XHR error classification and the fallback policy."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import DNSError, Timeout

from alt_ani_cli.cli import _record_extraction_failure
from alt_ani_cli.config import GUEST_AUTH, SHINDEN_API_BASE
from alt_ani_cli.errors import ShindenXhrError
from alt_ani_cli.shinden.api import XhrOutcome, resolve_embed, stop_message, xhr_outcome

_BASE = f"{SHINDEN_API_BASE}/xhr/42"
_LOAD_URL = f"{_BASE}/player_load?auth={GUEST_AUTH}"
_SHOW_URL = f"{_BASE}/player_show?auth={GUEST_AUTH}&width=0&height=-1"
_IFRAME_HTML = '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=9999"></iframe></body></html>'
_LIVE_LOAD_BODY = (Path(__file__).parent.parent / "fixtures" / "shinden" / "xhr_player_load_ok.txt").read_text(encoding="utf-8")
_SECRET_MESSAGE = "token=abc123secret cf_clearance=zzz"


def _resp(status_code: int = 200, text: str = "", headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


def _error_json(code: str | None = None, retry_after: object = None, message: str = _SECRET_MESSAGE) -> str:
    error: dict = {"message": message}
    if code is not None:
        error["code"] = code
    if retry_after is not None:
        error["retry_after"] = retry_after
    return json.dumps({"error": error})


def _client(*responses) -> MagicMock:
    client = MagicMock()
    client.get.side_effect = list(responses)
    return client


def _resolve(client, **kw):
    with patch("alt_ani_cli.shinden.api.time.sleep") as mock_sleep:
        try:
            return resolve_embed(client, "42", **kw), mock_sleep
        except ShindenXhrError as exc:
            exc.mock_sleep = mock_sleep
            raise


def _xhr_error(**fields) -> ShindenXhrError:
    defaults = dict(stage="player_load", category="http_error", reason="http_status")
    return ShindenXhrError("x", **(defaults | fields))


@pytest.mark.unit
class TestPlayerLoadWait:
    def test_live_fixture_body_is_accepted(self):
        client = _client(_resp(text=_LIVE_LOAD_BODY), _resp(text=_IFRAME_HTML))
        result, mock_sleep = _resolve(client, min_sleep_seconds=0.0)
        mock_sleep.assert_called_once_with(int(_LIVE_LOAD_BODY))
        assert result.url == "https://video.sibnet.ru/shell.php?videoid=9999"

    def test_sleep_happens_between_load_and_show(self):
        order: list[str] = []
        client = MagicMock()
        client.get.side_effect = lambda url, **kw: (order.append(url), _resp(text="5" if url == _LOAD_URL else _IFRAME_HTML))[1]
        with patch("alt_ani_cli.shinden.api.time.sleep", side_effect=lambda s: order.append("sleep")):
            resolve_embed(client, "42", min_sleep_seconds=0.0)
        assert order == [_LOAD_URL, "sleep", _SHOW_URL]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [("7", 7), (" 7\n", 7), ("0", 0), ("30", 30), ("7.0", 7), ("1e1", 10), ("12", 12)],
    )
    def test_valid_whole_seconds(self, body, expected):
        client = _client(_resp(text=body), _resp(text=_IFRAME_HTML))
        _, mock_sleep = _resolve(client, min_sleep_seconds=0.0)
        mock_sleep.assert_called_once_with(expected)

    def test_configured_minimum_lengthens_a_shorter_server_wait(self):
        client = _client(_resp(text="0"), _resp(text=_IFRAME_HTML))
        _, mock_sleep = _resolve(client, min_sleep_seconds=5.0)
        mock_sleep.assert_called_once_with(5.0)

    def test_server_wait_wins_over_a_shorter_minimum(self):
        client = _client(_resp(text="12"), _resp(text=_IFRAME_HTML))
        _, mock_sleep = _resolve(client, min_sleep_seconds=5.0)
        mock_sleep.assert_called_once_with(12)

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            ("", "wait_missing"),
            ("   ", "wait_missing"),
            ("ok", "wait_not_a_number"),
            ("true", "wait_not_a_number"),
            ("NaN", "wait_not_a_number"),
            ("Infinity", "wait_not_a_number"),
            ('"7"', "wait_not_a_number"),
            ('{"czas": 7}', "wait_not_a_number"),
            ("[7]", "wait_not_a_number"),
            ("7.5abc", "wait_not_a_number"),
            ("<html>Just a moment...</html>", "wait_not_a_number"),
            ("7.5", "wait_not_whole_seconds"),
            ("-1", "wait_negative"),
            ("31", "wait_above_limit"),
            ("1e9", "wait_above_limit"),
            ("9" * 400, "wait_above_limit"),
        ],
    )
    def test_invalid_wait_is_parser_drift_without_sleep_or_player_show(self, body, reason):
        client = _client(_resp(text=body))
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(client, min_sleep_seconds=5.0)
        err = exc_info.value
        assert (err.stage, err.category, err.reason, err.http_status) == ("player_load", "parser_drift", reason, 200)
        assert client.get.call_count == 1
        err.mock_sleep.assert_not_called()
        assert xhr_outcome(err) is XhrOutcome.API_PROTOCOL_ERROR

    def test_invalid_wait_message_never_echoes_the_body(self):
        client = _client(_resp(text="secret-body-token"))
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(client)
        assert "secret-body-token" not in str(exc_info.value)
        assert "secret-body-token" not in stop_message(exc_info.value)


@pytest.mark.unit
class TestHttpErrors:
    @pytest.mark.parametrize("stage", ["player_load", "player_show"])
    @pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 404])
    @pytest.mark.parametrize(
        "body",
        ["", "<html>error</html>", '{"foo": 1}', '{"error": "not-a-dict"}', "{broken json"],
        ids=["empty", "html", "json-without-error", "error-not-dict", "broken-json"],
    )
    def test_status_without_usable_error_json(self, stage, status, body):
        failing = _resp(status, body)
        responses = (failing,) if stage == "player_load" else (_resp(text="5"), failing)
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(*responses))
        err = exc_info.value
        assert (err.stage, err.category, err.http_status, err.api_code, err.reason) == (
            stage,
            "http_error",
            status,
            None,
            "http_status",
        )

    @pytest.mark.parametrize("stage", ["player_load", "player_show"])
    @pytest.mark.parametrize("status", [401, 403, 429, 503])
    def test_status_with_error_json(self, stage, status):
        failing = _resp(status, _error_json("turnstile", retry_after=30))
        responses = (failing,) if stage == "player_load" else (_resp(text="5"), failing)
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(*responses))
        err = exc_info.value
        assert (err.stage, err.http_status, err.api_code, err.retry_after, err.reason) == (
            stage,
            status,
            "turnstile",
            30,
            "api_error",
        )

    @pytest.mark.parametrize("status", [200, 403])
    def test_invalid_token_blocks_without_fallback(self, status):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(status, _error_json("invalid_token"))))
        err = exc_info.value
        assert (err.http_status, err.api_code) == (status, "invalid_token")
        assert xhr_outcome(err) is XhrOutcome.ACCESS_BLOCKED
        assert "blocked" in stop_message(err)

    def test_200_with_error_json_is_an_api_error(self):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(200, _error_json("turnstile"))))
        assert (exc_info.value.http_status, exc_info.value.api_code) == (200, "turnstile")

    @pytest.mark.parametrize("code", [123, None, "x" * 65, "bad code!", ["turnstile"]])
    def test_malformed_api_code_is_dropped(self, code):
        body = json.dumps({"error": {"code": code}})
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(403, body)))
        assert exc_info.value.api_code is None

    def test_server_message_is_never_in_exception_or_stop_message(self):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(429, _error_json("konto_robot"))))
        assert _SECRET_MESSAGE not in str(exc_info.value)
        assert "abc123secret" not in stop_message(exc_info.value)

    def test_url_with_guest_auth_is_never_in_messages(self):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(503, "")))
        assert GUEST_AUTH not in str(exc_info.value)
        assert GUEST_AUTH not in stop_message(exc_info.value)


@pytest.mark.unit
class TestRetryAfter:
    def _retry_after(self, headers=None, body=""):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(429, body, headers)))
        exc_info.value.mock_sleep.assert_not_called()
        return exc_info.value.retry_after

    def test_header_seconds(self):
        assert self._retry_after({"Retry-After": "120"}) == 120

    def test_header_http_date(self):
        when = datetime.now(UTC) + timedelta(seconds=90)
        assert 85 <= self._retry_after({"Retry-After": format_datetime(when, usegmt=True)}) <= 91

    def test_header_date_in_the_past_is_zero(self):
        when = datetime.now(UTC) - timedelta(hours=1)
        assert self._retry_after({"Retry-After": format_datetime(when, usegmt=True)}) == 0

    def test_header_date_without_zone_is_read_as_utc(self):
        assert self._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 -0000"}) == 0

    @pytest.mark.parametrize("value", ["soon", "-5", ""])
    def test_header_garbage_is_ignored(self, value):
        assert self._retry_after({"Retry-After": value}) is None

    @pytest.mark.parametrize(("value", "expected"), [(60, 60), (2.5, 3), ("45", 45), (-1, None), (True, None), ("x", None)])
    def test_json_retry_after(self, value, expected):
        assert self._retry_after(body=_error_json("vpnp_limit", retry_after=value)) == expected

    def test_header_wins_over_json(self):
        assert self._retry_after({"Retry-After": "10"}, _error_json(retry_after=99)) == 10

    def test_invalid_header_falls_back_to_json(self):
        assert self._retry_after({"Retry-After": "soon"}, _error_json(retry_after=99)) == 99

    def test_stop_message_shows_wait_time(self):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(429, "", {"Retry-After": "120"})))
        assert "120 s" in stop_message(exc_info.value)


@pytest.mark.unit
class TestTransportErrors:
    @pytest.mark.parametrize(
        ("exc", "category"),
        [
            (Timeout("timed out"), "timeout"),
            (DNSError("could not resolve host"), "network_error"),
            (CurlConnectionError("connection refused"), "network_error"),
        ],
    )
    @pytest.mark.parametrize("stage", ["player_load", "player_show"])
    def test_transport_error_is_classified(self, exc, category, stage):
        client = _client(exc) if stage == "player_load" else _client(_resp(text="5"), exc)
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(client)
        err = exc_info.value
        assert (err.stage, err.category, err.reason, err.http_status) == (stage, category, "transport", None)
        assert err.__cause__ is exc
        assert xhr_outcome(err) is XhrOutcome.SERVICE_UNAVAILABLE


@pytest.mark.unit
class TestPlayerShowIframe:
    @pytest.mark.parametrize(
        ("html", "reason"),
        [
            ("<html><body>secret-body</body></html>", "no_iframe"),
            ("<iframe></iframe>", "iframe_no_src"),
            ('<iframe src="javascript:alert(1)"></iframe>', "unexpected_iframe_src"),
        ],
    )
    def test_missing_or_bad_iframe_is_no_stream_url(self, html, reason):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(text="5"), _resp(text=html)))
        err = exc_info.value
        assert (err.stage, err.category, err.reason) == ("player_show", "no_stream_url", reason)
        assert "secret-body" not in str(err)
        assert xhr_outcome(err) is XhrOutcome.NEXT_PLAYER

    def test_protocol_relative_src_gets_https(self):
        result, _ = _resolve(_client(_resp(text="5"), _resp(text='<iframe src="//cdn.example.com/embed/abc"></iframe>')))
        assert result.url == "https://cdn.example.com/embed/abc"

    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            ("https://example.com/embed", "https://example.com/embed"),
            ("http://example.com/embed", "http://example.com/embed"),
            ("//example.com/embed", "https://example.com/embed"),
        ],
    )
    def test_absolute_web_src_is_accepted(self, src, expected):
        result, _ = _resolve(_client(_resp(text="5"), _resp(text=f'<iframe src="{src}"></iframe>')))
        assert result.url == expected

    @pytest.mark.parametrize(
        "src",
        [
            "httpfoo",
            "https-not-a-url",
            "https://",
            "javascript:alert(1)",
            "http://[::1/embed",
            "//[bad/embed",
            "ftp://example.com/x",
        ],
    )
    def test_malformed_src_is_unexpected(self, src):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(text="5"), _resp(text=f'<iframe src="{src}"></iframe>')))
        err = exc_info.value
        assert (err.stage, err.category, err.reason) == ("player_show", "no_stream_url", "unexpected_iframe_src")
        assert xhr_outcome(err) is XhrOutcome.NEXT_PLAYER

    @pytest.mark.parametrize(
        "src",
        [
            "javascript:SECRETPATH/x",
            "https:///embed?token=SECRETPATH",
            "httpx://example.com/embed#SECRETPATH",
            "http://[::1/SECRETPATH?t=SECRETPATH",
        ],
    )
    def test_malformed_src_never_leaks(self, src, diag_events, capsys):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(text="5"), _resp(text=f'<iframe src="{src}"></iframe>')))
        err = exc_info.value
        assert err.reason == "unexpected_iframe_src"
        candidate = MagicMock(online_id="42", player="Example")
        _record_extraction_failure(None, candidate, err, None, 0.0, 1)
        assert diag_events
        assert "SECRETPATH" not in str(err)
        assert "SECRETPATH" not in stop_message(err)
        assert "SECRETPATH" not in repr(diag_events)
        assert "SECRETPATH" not in capsys.readouterr().out


@pytest.mark.unit
class TestXhrOutcome:
    @pytest.mark.parametrize(
        "code", ["turnstile", "auth_required", "invalid_token", "konto_robot", "vpnp_limit", "ochrona_godz"]
    )
    @pytest.mark.parametrize("status", [200, 400, 404, 500, 503])
    def test_protection_codes_block_regardless_of_status(self, code, status):
        assert xhr_outcome(_xhr_error(http_status=status, api_code=code)) is XhrOutcome.ACCESS_BLOCKED

    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_auth_and_rate_limit_statuses_block(self, status):
        assert xhr_outcome(_xhr_error(http_status=status)) is XhrOutcome.ACCESS_BLOCKED

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_server_errors_are_unavailability(self, status):
        assert xhr_outcome(_xhr_error(http_status=status)) is XhrOutcome.SERVICE_UNAVAILABLE

    def test_server_error_with_unknown_code_is_still_unavailability(self):
        assert xhr_outcome(_xhr_error(http_status=500, api_code="brak_playera")) is XhrOutcome.SERVICE_UNAVAILABLE

    @pytest.mark.parametrize("category", ["timeout", "network_error"])
    def test_transport_is_unavailability(self, category):
        assert xhr_outcome(_xhr_error(category=category, reason="transport")) is XhrOutcome.SERVICE_UNAVAILABLE

    @pytest.mark.parametrize("status", [404, 410])
    def test_gone_player_moves_on(self, status):
        assert xhr_outcome(_xhr_error(http_status=status)) is XhrOutcome.NEXT_PLAYER

    @pytest.mark.parametrize("status", [200, 400])
    def test_unrecognised_code_below_5xx_is_protocol_error(self, status):
        assert xhr_outcome(_xhr_error(http_status=status, api_code="brak_playera")) is XhrOutcome.API_PROTOCOL_ERROR

    @pytest.mark.parametrize("status", [404, 410])
    def test_gone_player_with_unrecognised_code_still_moves_on(self, status):
        assert xhr_outcome(_xhr_error(http_status=status, api_code="brak_playera")) is XhrOutcome.NEXT_PLAYER

    @pytest.mark.parametrize("status", [400, 405, 418])
    def test_other_4xx_without_code_is_protocol_error(self, status):
        assert xhr_outcome(_xhr_error(http_status=status)) is XhrOutcome.API_PROTOCOL_ERROR

    def test_player_show_no_stream_moves_on(self):
        err = _xhr_error(stage="player_show", category="no_stream_url", reason="no_iframe", http_status=200)
        assert xhr_outcome(err) is XhrOutcome.NEXT_PLAYER

    def test_player_load_parser_drift_stops(self):
        err = _xhr_error(category="parser_drift", reason="wait_above_limit", http_status=200)
        assert xhr_outcome(err) is XhrOutcome.API_PROTOCOL_ERROR


@pytest.mark.unit
class TestStopMessage:
    def test_block_and_unavailability_are_worded_differently(self):
        blocked = stop_message(_xhr_error(http_status=429))
        unavailable = stop_message(_xhr_error(http_status=503))
        assert "blocked" in blocked
        assert "unavailable" in unavailable
        assert "antibot" not in unavailable.lower()
        assert "block" not in unavailable.lower()

    def test_message_carries_stage_status_and_code(self):
        msg = stop_message(_xhr_error(stage="player_show", http_status=403, api_code="ochrona_godz"))
        assert "player_show" in msg
        assert "HTTP 403" in msg
        assert "code=ochrona_godz" in msg

    def test_no_wait_hint_without_retry_after(self):
        assert " s." not in stop_message(_xhr_error(http_status=429))
