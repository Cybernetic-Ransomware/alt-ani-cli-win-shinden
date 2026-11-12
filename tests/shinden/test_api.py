"""resolve_embed: player_load wait contract, XHR error classification and the fallback policy."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

from alt_ani_cli.event_renderer import ConsoleEvents
from alt_ani_core.config import GUEST_AUTH, SHINDEN_API_BASE, SHINDEN_XHR_BASE
from alt_ani_core.errors import OperationCancelled, ShindenXhrError
from alt_ani_core.events import SourceAttemptFailed
from alt_ani_core.models import EpisodeRow
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.shinden.api import XhrOutcome, resolve_embed, stop_message, xhr_outcome
from alt_ani_core.shinden.xhr import BACKEND_API4, BACKEND_NEW, EpisodeXhr, EpisodeXhrContext
from alt_ani_core.testing import RecordingCancel
from alt_ani_core.transport.api import NetworkError, TransportError, TransportTimeout

from .conftest import EPISODE_URL, TRAP_ID

_BASE = f"{SHINDEN_API_BASE}/xhr/42"
_LOAD_URL = f"{_BASE}/player_load?auth={GUEST_AUTH}"
_SHOW_URL = f"{_BASE}/player_show?auth={GUEST_AUTH}&width=0&height=-1"
_IFRAME_HTML = '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=9999"></iframe></body></html>'
_LIVE_LOAD_BODY = (Path(__file__).parent.parent / "fixtures" / "shinden" / "xhr_player_load_ok.txt").read_text(encoding="utf-8")
_SECRET_MESSAGE = "token=abc123secret cf_clearance=zzz"
_EPISODE_URL = "https://shinden.pl/episode/1-example/view/2"


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


def _api4_xhr() -> EpisodeXhr:
    return EpisodeXhr(episode_url=_EPISODE_URL, player_ids=frozenset({"42"}), trap_id=None, backend=BACKEND_API4)


def _resolve(client, **kw):
    kw.setdefault("xhr", _api4_xhr())
    cancel = RecordingCancel()
    try:
        return resolve_embed(client, "42", cancel=cancel, **kw), cancel
    except ShindenXhrError as exc:
        exc.cancel = cancel
        raise


def _xhr_error(**fields) -> ShindenXhrError:
    defaults = dict(stage="player_load", category="http_error", reason="http_status")
    return ShindenXhrError("x", **(defaults | fields))


@pytest.mark.unit
class TestPlayerLoadWait:
    def test_live_fixture_body_is_accepted(self):
        client = _client(_resp(text=_LIVE_LOAD_BODY), _resp(text=_IFRAME_HTML))
        result, cancel = _resolve(client, min_sleep_seconds=0.0)
        assert cancel.waits == [int(_LIVE_LOAD_BODY)]
        assert result.url == "https://video.sibnet.ru/shell.php?videoid=9999"

    def test_sleep_happens_between_load_and_show(self):
        order: list[str] = []
        client = MagicMock()
        client.get.side_effect = lambda url, **kw: (order.append(url), _resp(text="5" if url == _LOAD_URL else _IFRAME_HTML))[1]
        with patch("alt_ani_core.cancel.CancelToken.wait", side_effect=lambda s: order.append("sleep")):
            resolve_embed(client, "42", xhr=_api4_xhr(), min_sleep_seconds=0.0)
        assert order == [_LOAD_URL, "sleep", _SHOW_URL]

    @pytest.mark.parametrize(
        ("body", "expected"),
        [("7", 7), (" 7\n", 7), ("0", 0), ("30", 30), ("7.0", 7), ("1e1", 10), ("12", 12)],
    )
    def test_valid_whole_seconds(self, body, expected):
        client = _client(_resp(text=body), _resp(text=_IFRAME_HTML))
        _, cancel = _resolve(client, min_sleep_seconds=0.0)
        assert cancel.waits == [expected]

    def test_configured_minimum_lengthens_a_shorter_server_wait(self):
        client = _client(_resp(text="0"), _resp(text=_IFRAME_HTML))
        _, cancel = _resolve(client, min_sleep_seconds=5.0)
        assert cancel.waits == [5.0]

    def test_server_wait_wins_over_a_shorter_minimum(self):
        client = _client(_resp(text="12"), _resp(text=_IFRAME_HTML))
        _, cancel = _resolve(client, min_sleep_seconds=5.0)
        assert cancel.waits == [12]

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
        assert err.cancel.waits == []
        assert xhr_outcome(err) is XhrOutcome.API_PROTOCOL_ERROR

    def test_invalid_wait_message_never_echoes_the_body(self):
        client = _client(_resp(text="secret-body-token"))
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(client)
        assert "secret-body-token" not in str(exc_info.value)
        assert "secret-body-token" not in stop_message(exc_info.value)


@pytest.mark.unit
class TestCancellation:
    def test_cancel_during_the_antibot_wait_never_requests_player_show(self):
        client = _client(_resp(text="5"), _resp(text=_IFRAME_HTML))
        cancel = RecordingCancel(cancel_on_wait=1)
        with pytest.raises(OperationCancelled):
            resolve_embed(client, "42", xhr=_api4_xhr(), cancel=cancel)
        assert [c.args[0] for c in client.get.call_args_list] == [_LOAD_URL]
        assert cancel.waits == [5.0]

    def test_token_cancelled_before_the_call_stops_at_the_wait(self):
        client = _client(_resp(text="7"), _resp(text=_IFRAME_HTML))
        cancel = RecordingCancel()
        cancel.cancel()
        with pytest.raises(OperationCancelled):
            resolve_embed(client, "42", xhr=_api4_xhr(), min_sleep_seconds=0.0, cancel=cancel)
        assert _SHOW_URL not in [c.args[0] for c in client.get.call_args_list]

    def test_cancel_after_invalid_token_skips_the_page_refresh(self):
        xhr = _new_xhr()
        cancel = RecordingCancel()
        client = MagicMock()
        client.get.side_effect = lambda url, **kw: (cancel.cancel(), _expired())[1]
        with pytest.raises(OperationCancelled):
            resolve_embed(client, _PAGE_ID, xhr=xhr, cancel=cancel)
        assert client.get.call_count == 1
        assert xhr.refresh_used is False


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
        assert exc_info.value.cancel.waits == []
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
            (TransportTimeout(url=_LOAD_URL), "timeout"),
            (NetworkError(url=_LOAD_URL), "network_error"),
            (TransportError("connection refused", url=_LOAD_URL), "network_error"),
        ],
    )
    @pytest.mark.parametrize("stage", ["player_load", "player_show"])
    def test_transport_error_is_classified(self, exc, category, stage):
        client = _client(exc) if stage == "player_load" else _client(_resp(text="5"), exc)
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(client)
        err = exc_info.value
        assert (err.stage, err.category, err.reason, err.http_status) == (stage, category, "transport", None)
        # the transport error keeps the auth URL on .url, so nothing of it stays on the error
        assert err.__cause__ is None
        assert err.__context__ is None
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
    def test_malformed_src_never_leaks(self, src, diag_events, capsys, make_engine, sink):
        with pytest.raises(ShindenXhrError) as exc_info:
            _resolve(_client(_resp(text="5"), _resp(text=f'<iframe src="{src}"></iframe>')))
        err = exc_info.value
        assert err.reason == "unexpected_iframe_src"
        candidate = MagicMock(online_id="42", player="Example")
        session = EpisodeSession(make_engine(), 0, EpisodeRow(1, "Example", _EPISODE_URL), [], _api4_xhr())
        session._record_failure(None, candidate, err, 0.0)
        assert diag_events
        failed = sink.of(SourceAttemptFailed)
        assert len(failed) == 1
        ConsoleEvents().emit(failed[0])
        assert "SECRETPATH" not in repr(sink.events)
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


_PAGE_ID = "1825161"
_OLD_TOKEN = "T2xkRXBpc29kZVRva2Vu+/8="
_NEW_TOKEN = "TmV3RXBpc29kZVRva2Vu+/8="


def _new_xhr(token: str = _OLD_TOKEN, ids=(_PAGE_ID, "1825160"), trap: str | None = TRAP_ID) -> EpisodeXhr:
    return EpisodeXhr(
        episode_url=EPISODE_URL,
        player_ids=frozenset(ids),
        trap_id=trap,
        backend=BACKEND_NEW,
        context=EpisodeXhrContext(endpoint=SHINDEN_XHR_BASE, auth=token),
    )


def _new_urls(online_id: str, token: str) -> tuple[str, str]:
    auth = "auth=" + quote(token, safe="")
    base = f"{SHINDEN_XHR_BASE}/{online_id}"
    return f"{base}/player_load?{auth}", f"{base}/player_show?{auth}&width=0&height=-1"


def _urls(client) -> list[str]:
    return [c.args[0] for c in client.get.call_args_list]


def _run(client, xhr, online_id=_PAGE_ID):
    return resolve_embed(client, online_id, xhr=xhr, cancel=RecordingCancel())


def _expired() -> MagicMock:
    return _resp(403, _error_json("invalid_token"))


_CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head></html>"


@pytest.mark.unit
class TestNewBackend:
    def test_token_is_url_encoded_exactly_once(self):
        client = _client(_resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, _new_xhr())
        load_url, show_url = _new_urls(_PAGE_ID, _OLD_TOKEN)
        assert _urls(client) == [load_url, show_url]
        assert load_url.endswith("auth=T2xkRXBpc29kZVRva2Vu%2B%2F8%3D")
        assert "%25" not in load_url

    def test_both_backends_share_the_parsing_and_return_the_same_embed(self):
        api4 = EpisodeXhr(EPISODE_URL, frozenset({_PAGE_ID}), None, BACKEND_API4)
        new = _run(_client(_resp(text="5"), _resp(text=_IFRAME_HTML)), _new_xhr())
        old = _run(_client(_resp(text="5"), _resp(text=_IFRAME_HTML)), api4)
        assert new == old

    def test_errors_carry_the_backend(self):
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(_client(_resp(503, "")), _new_xhr())
        assert exc_info.value.backend == BACKEND_NEW

    @pytest.mark.parametrize(
        "first",
        [
            _resp(403, _error_json("turnstile")),
            _resp(403, _error_json("konto_robot")),
            _resp(401, ""),
            _resp(403, ""),
            _resp(429, "", {"Retry-After": "30"}),
            _resp(503, ""),
            _resp(404, ""),
            _resp(text="7.5"),
            TransportTimeout(),
        ],
        ids=["turnstile", "konto_robot", "401", "403", "429", "503", "404", "drift", "timeout"],
    )
    def test_no_new_backend_answer_ever_leads_to_api4_or_a_refresh(self, first):
        client = _client(first)
        xhr = _new_xhr()
        with pytest.raises(ShindenXhrError):
            _run(client, xhr)
        assert len(_urls(client)) == 1
        assert all(url.startswith(SHINDEN_XHR_BASE) for url in _urls(client))
        assert (xhr.backend, xhr.refresh_used) == (BACKEND_NEW, False)

    def test_api4_invalid_token_is_not_refreshed(self):
        client = _client(_expired())
        xhr = EpisodeXhr(EPISODE_URL, frozenset({_PAGE_ID}), None, BACKEND_API4)
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr)
        assert xhr_outcome(exc_info.value) is XhrOutcome.ACCESS_BLOCKED
        assert client.get.call_count == 1


@pytest.mark.unit
class TestPlayerGuard:
    def test_honeypot_never_gets_a_request(self):
        client = MagicMock()
        with pytest.raises(AssertionError):
            _run(client, _new_xhr(), online_id=TRAP_ID)
        client.get.assert_not_called()

    def test_player_from_another_page_never_gets_a_request(self):
        client = MagicMock()
        with pytest.raises(AssertionError):
            _run(client, _new_xhr(), online_id="999")
        client.get.assert_not_called()

    def test_forced_api4_keeps_the_honeypot_guard(self):
        client = MagicMock()
        xhr = EpisodeXhr(EPISODE_URL, frozenset({_PAGE_ID}), TRAP_ID, BACKEND_API4, forced=True)
        with pytest.raises(AssertionError):
            _run(client, xhr, online_id=TRAP_ID)
        client.get.assert_not_called()


@pytest.mark.unit
class TestTokenRefresh:
    def test_invalid_token_refreshes_once_and_retries_on_the_new_backend(self, episode_page, diag_events):
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)), _resp(text="5"), _resp(text=_IFRAME_HTML))
        xhr = _new_xhr()
        embed = _run(client, xhr)
        old_load, _ = _new_urls(_PAGE_ID, _OLD_TOKEN)
        new_load, new_show = _new_urls(_PAGE_ID, _NEW_TOKEN)
        assert _urls(client) == [old_load, EPISODE_URL, new_load, new_show]
        assert embed.url == "https://video.sibnet.ru/shell.php?videoid=9999"
        assert xhr.context is not None
        assert xhr.context.auth == _NEW_TOKEN
        assert (xhr.refresh_used, xhr.list_refreshed, xhr.backend) == (True, True, BACKEND_NEW)
        refresh = {"action": "token_refresh", "backend": BACKEND_NEW, "reason": None, "online_id": _PAGE_ID}
        assert ("shinden_xhr_context", refresh) in diag_events

    def test_invalid_token_on_player_show_also_refreshes(self, episode_page):
        client = _client(
            _resp(text="5"),
            _expired(),
            _resp(text=episode_page(token=_NEW_TOKEN)),
            _resp(text="5"),
            _resp(text=_IFRAME_HTML),
        )
        _run(client, _new_xhr())
        assert client.get.call_count == 5

    def test_repeated_invalid_token_stops_without_api4(self, episode_page):
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)), _expired())
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, _new_xhr())
        assert xhr_outcome(exc_info.value) is XhrOutcome.ACCESS_BLOCKED
        assert client.get.call_count == 3
        assert not any("api4" in url for url in _urls(client))

    def test_one_refresh_per_operation(self):
        client = _client(_expired())
        xhr = _new_xhr()
        xhr.refresh_used = True
        with pytest.raises(ShindenXhrError):
            _run(client, xhr)
        assert client.get.call_count == 1

    def test_next_operation_gets_a_fresh_budget(self, episode_page):
        xhr = _new_xhr()
        xhr.refresh_used = True
        xhr.begin_operation()
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)), _resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, xhr)
        assert client.get.call_count == 4

    def test_refreshed_list_drops_a_player_that_is_gone(self, episode_page):
        xhr = _new_xhr(ids=(_PAGE_ID, "777"))
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)))
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr, online_id="777")
        err = exc_info.value
        assert (err.stage, err.reason) == ("token_refresh", "player_not_listed")
        assert xhr_outcome(err) is XhrOutcome.PLAYERS_CHANGED
        assert client.get.call_count == 2

    def test_player_that_became_the_honeypot_is_never_loaded(self, episode_page):
        xhr = _new_xhr(ids=(_PAGE_ID, TRAP_ID), trap="1")
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)))
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr, online_id=TRAP_ID)
        assert exc_info.value.reason == "player_is_honeypot"
        assert xhr_outcome(exc_info.value) is XhrOutcome.PLAYERS_CHANGED
        assert client.get.call_count == 2

    def test_old_list_candidate_missing_after_refresh_gets_no_request(self, episode_page):
        xhr = _new_xhr(ids=(_PAGE_ID, "777"))
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)), _resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, xhr)
        client.get.reset_mock()
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr, online_id="777")
        assert xhr_outcome(exc_info.value) is XhrOutcome.PLAYERS_CHANGED
        client.get.assert_not_called()

    @pytest.mark.parametrize(
        ("page", "outcome", "reason"),
        [
            (_resp(404, ""), XhrOutcome.API_PROTOCOL_ERROR, "http_status"),
            (_resp(410, ""), XhrOutcome.API_PROTOCOL_ERROR, "http_status"),
            (_resp(403, ""), XhrOutcome.ACCESS_BLOCKED, "http_status"),
            (_resp(503, ""), XhrOutcome.SERVICE_UNAVAILABLE, "http_status"),
            (TransportTimeout(), XhrOutcome.SERVICE_UNAVAILABLE, "transport"),
            (_resp(200, _CHALLENGE_PAGE), XhrOutcome.API_PROTOCOL_ERROR, "refresh_page_invalid"),
        ],
        ids=["404", "410", "403", "503", "timeout", "challenge"],
    )
    def test_refresh_failure_is_never_one_players_failure(self, page, outcome, reason):
        client = _client(_expired(), page)
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, _new_xhr())
        err = exc_info.value
        assert (err.stage, err.reason) == ("token_refresh", reason)
        assert xhr_outcome(err) is outcome
        assert client.get.call_count == 2

    @pytest.mark.parametrize(
        ("kw", "reason"), [({"token": None}, "refresh_auth_missing"), ({"xhr": None}, "refresh_endpoint_missing")]
    )
    def test_refreshed_page_without_context_stops_without_api4(self, episode_page, kw, reason):
        client = _client(_expired(), _resp(text=episode_page(**kw)))
        xhr = _new_xhr()
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr)
        assert exc_info.value.reason == reason
        assert xhr_outcome(exc_info.value) is XhrOutcome.API_PROTOCOL_ERROR
        assert xhr.backend == BACKEND_NEW
        assert client.get.call_count == 2

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda html: html.replace(f'"pulapka": {TRAP_ID}', '"pulapka_v2": 1', 1),
            lambda html: html.replace(f'"pulapka": {TRAP_ID}', '"pulapka": 1234', 1),
        ],
        ids=["missing", "hidden_row_mismatch"],
    )
    def test_refreshed_page_with_broken_honeypot_contract_stops(self, episode_page, mutate):
        client = _client(_expired(), _resp(text=mutate(episode_page(token=_NEW_TOKEN))))
        xhr = _new_xhr()
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr)
        assert exc_info.value.reason == "refresh_page_invalid"
        assert xhr_outcome(exc_info.value) is XhrOutcome.API_PROTOCOL_ERROR
        assert xhr.context is not None
        assert xhr.context.auth == _OLD_TOKEN
        assert client.get.call_count == 2

    def test_refresh_transport_error_keeps_no_exception_chain(self):
        client = _client(_expired(), NetworkError(url=f"{EPISODE_URL}?auth={_OLD_TOKEN}"))
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, _new_xhr())
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert exc_info.value.category == "network_error"


@pytest.mark.unit
class TestNoSecretLeak:
    def test_token_never_reaches_messages_diagnostics_or_errors(self, episode_page, diag_events, capsys):
        leak = f"https://shinden.pl/xhr/{_PAGE_ID}/player_load?auth={_OLD_TOKEN}"
        client = _client(_expired(), _resp(text=episode_page(token=_NEW_TOKEN)), TransportTimeout(url=leak))
        xhr = _new_xhr()
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr)
        err = exc_info.value
        texts = [str(err), repr(err), stop_message(err), repr(diag_events), repr(xhr), capsys.readouterr().out]
        for token in (_OLD_TOKEN, _NEW_TOKEN, quote(_OLD_TOKEN, safe=""), quote(_NEW_TOKEN, safe="")):
            assert all(token not in text for text in texts)
        assert "shinden.pl/xhr" not in stop_message(err)
        assert err.__context__ is None
        assert err.__cause__ is None


@pytest.mark.unit
class TestRefreshStageOutcome:
    @pytest.mark.parametrize(
        ("fields", "outcome"),
        [
            ({"http_status": 404}, XhrOutcome.API_PROTOCOL_ERROR),
            ({"http_status": 410}, XhrOutcome.API_PROTOCOL_ERROR),
            ({"category": "no_stream_url", "reason": "x"}, XhrOutcome.API_PROTOCOL_ERROR),
            ({"http_status": 403}, XhrOutcome.ACCESS_BLOCKED),
            ({"http_status": 429}, XhrOutcome.ACCESS_BLOCKED),
            ({"http_status": 502}, XhrOutcome.SERVICE_UNAVAILABLE),
            ({"category": "timeout", "reason": "transport"}, XhrOutcome.SERVICE_UNAVAILABLE),
            ({"category": "player_list_changed", "reason": "player_not_listed"}, XhrOutcome.PLAYERS_CHANGED),
            ({"category": "player_list_changed", "reason": "player_is_honeypot"}, XhrOutcome.PLAYERS_CHANGED),
        ],
    )
    def test_token_refresh_stage(self, fields, outcome):
        assert xhr_outcome(_xhr_error(stage="token_refresh", **fields)) is outcome

    def test_players_changed_has_its_own_stop_message(self):
        msg = stop_message(_xhr_error(stage="token_refresh", category="player_list_changed", reason="player_not_listed"))
        assert "player list" in msg
        assert "reason=player_not_listed" in msg


@pytest.mark.unit
class TestTarget:
    def test_api4_goes_to_the_legacy_endpoint(self):
        client = _client(_resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, EpisodeXhr(EPISODE_URL, frozenset({_PAGE_ID}), None, BACKEND_API4))
        assert all(url.startswith(f"{SHINDEN_API_BASE}/xhr/") for url in _urls(client))

    def test_api4_ignores_a_stray_context(self):
        xhr = _new_xhr()
        xhr.backend = BACKEND_API4
        client = _client(_resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, xhr)
        assert all(url.startswith(f"{SHINDEN_API_BASE}/xhr/") and _OLD_TOKEN not in url for url in _urls(client))

    def test_new_backend_with_context_goes_to_the_new_endpoint(self):
        client = _client(_resp(text="5"), _resp(text=_IFRAME_HTML))
        _run(client, _new_xhr())
        assert _urls(client) == list(_new_urls(_PAGE_ID, _OLD_TOKEN))

    @pytest.mark.parametrize(
        ("backend", "with_context", "reason"),
        [
            (BACKEND_NEW, False, "context_missing"),
            ("unknown", False, "backend_unknown"),
            ("unknown", True, "backend_unknown"),
        ],
        ids=["new-without-context", "unknown", "unknown-with-context"],
    )
    def test_inconsistent_state_stops_before_any_request(self, backend, with_context, reason):
        xhr = _new_xhr()
        xhr.backend = backend
        if not with_context:
            xhr.context = None
        client = MagicMock()
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, xhr)
        client.get.assert_not_called()
        assert exc_info.value.reason == reason
        assert xhr_outcome(exc_info.value) is XhrOutcome.API_PROTOCOL_ERROR
