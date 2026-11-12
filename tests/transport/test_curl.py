"""curl_cffi adapter: exception translation, URL-free error text and one curl session per opened profile."""

from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests import exceptions as cffi_exceptions

from alt_ani_core.transport.api import (
    EXTRACTOR_PROFILE,
    DecodeError,
    HttpStatusError,
    NetworkError,
    SessionProfile,
    TransportError,
    TransportTimeout,
)
from alt_ani_core.transport.curl import CurlResponse, CurlSession, CurlTransport

_SECRET = "SECRETTOKEN123"
_URL = f"https://shinden.pl/xhr/42/player_load?auth={_SECRET}"
_PROFILE = SessionProfile(name="shinden", headers={"Referer": "https://shinden.pl/"}, cookies={"_rnd": "1"}, timeout=12.5)


def _http_error(status: int) -> cffi_exceptions.HTTPError:
    return cffi_exceptions.HTTPError(f"HTTP {status} for {_URL}", response=MagicMock(status_code=status))


def _raw(status_code: int = 200, text: str = "", headers: dict | None = None) -> MagicMock:
    raw = MagicMock(status_code=status_code, text=text, headers=headers or {})
    raw.json.return_value = {"ok": True}
    return raw


@pytest.fixture
def session_cls():
    """Each Session(...) call returns its own mock, so sessions can be told apart."""
    with patch("alt_ani_core.transport.curl.cffi_requests.Session", side_effect=lambda **kw: MagicMock()) as cls:
        yield cls


@pytest.fixture
def opened(session_cls):
    session = CurlTransport().open(_PROFILE)
    return session, session._session


@pytest.mark.unit
class TestExceptionTranslation:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (cffi_exceptions.Timeout(f"timed out: {_URL}"), TransportTimeout),
            (cffi_exceptions.ConnectTimeout(f"connect timed out: {_URL}"), TransportTimeout),
            (cffi_exceptions.DNSError(f"could not resolve {_URL}"), NetworkError),
            (cffi_exceptions.ConnectionError(f"refused {_URL}"), NetworkError),
            (cffi_exceptions.RequestException(f"failed {_URL}"), NetworkError),
            (cffi_exceptions.JSONDecodeError("Expecting value", "<html>", 0), DecodeError),
            (_http_error(503), HttpStatusError),
        ],
        ids=["timeout", "connect-timeout", "dns", "connection", "generic", "json", "http"],
    )
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_curl_exception_becomes_a_transport_error(self, opened, exc, expected, method):
        session, raw_session = opened
        getattr(raw_session, method).side_effect = exc
        with pytest.raises(TransportError) as exc_info:
            getattr(session, method)(_URL)
        err = exc_info.value
        assert type(err) is expected
        assert err.url == _URL
        # the curl message embeds the request URL, so the original exception must not stay reachable
        assert err.__cause__ is None
        assert err.__suppress_context__ is True

    def test_http_error_keeps_the_response_status(self, opened):
        session, raw_session = opened
        raw_session.get.side_effect = _http_error(429)
        with pytest.raises(HttpStatusError) as exc_info:
            session.get(_URL)
        assert exc_info.value.status == 429

    def test_http_error_without_a_response_has_status_zero(self, opened):
        session, raw_session = opened
        raw_session.get.side_effect = cffi_exceptions.HTTPError("no response")
        with pytest.raises(HttpStatusError) as exc_info:
            session.get(_URL)
        assert exc_info.value.status == 0

    def test_non_curl_exception_is_not_swallowed(self, opened):
        session, raw_session = opened
        raw_session.get.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            session.get(_URL)


@pytest.mark.unit
class TestNoUrlInErrorText:
    @pytest.mark.parametrize(
        "err",
        [
            TransportError(url=_URL),
            TransportTimeout(url=_URL),
            NetworkError(url=_URL),
            DecodeError(url=_URL),
            HttpStatusError(403, url=_URL),
        ],
        ids=["base", "timeout", "network", "decode", "http"],
    )
    def test_constructed_error_text_has_no_url(self, err):
        assert _URL not in str(err)
        assert _URL not in repr(err)
        assert _SECRET not in str(err)
        assert _SECRET not in repr(err)

    @pytest.mark.parametrize(
        "exc",
        [
            cffi_exceptions.Timeout(f"timed out: {_URL}"),
            cffi_exceptions.ConnectionError(f"refused {_URL}"),
            _http_error(403),
        ],
        ids=["timeout", "connection", "http"],
    )
    def test_translated_error_text_has_no_url(self, opened, exc):
        session, raw_session = opened
        raw_session.get.side_effect = exc
        with pytest.raises(TransportError) as exc_info:
            session.get(_URL)
        assert _SECRET not in str(exc_info.value)
        assert _SECRET not in repr(exc_info.value)

    def test_status_error_text_is_the_status_only(self):
        assert str(HttpStatusError(503, url=_URL)) == "HTTP 503"

    def test_response_errors_have_no_url(self):
        raw = _raw(status_code=500)
        raw.json.side_effect = ValueError(f"bad json from {_URL}")
        response = CurlResponse(raw, _URL)
        for call in (response.raise_for_status, response.json):
            with pytest.raises(TransportError) as exc_info:
                call()
            assert _SECRET not in str(exc_info.value)
            assert _SECRET not in repr(exc_info.value)
            assert exc_info.value.__cause__ is None


@pytest.mark.unit
class TestCurlResponse:
    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
    def test_raise_for_status_raises_from_400_up(self, status):
        with pytest.raises(HttpStatusError) as exc_info:
            CurlResponse(_raw(status_code=status), _URL).raise_for_status()
        assert (exc_info.value.status, exc_info.value.url) == (status, _URL)

    @pytest.mark.parametrize("status", [200, 204, 302, 399])
    def test_raise_for_status_passes_below_400(self, status):
        assert CurlResponse(_raw(status_code=status), _URL).raise_for_status() is None

    def test_exposes_status_text_headers_and_url(self):
        response = CurlResponse(_raw(status_code=201, text="body", headers={"Retry-After": "5"}), _URL)
        assert (response.status_code, response.text, response.headers, response.url) == (
            201,
            "body",
            {"Retry-After": "5"},
            _URL,
        )

    def test_json_returns_the_decoded_body(self):
        assert CurlResponse(_raw(), _URL).json() == {"ok": True}

    def test_invalid_json_is_a_decode_error(self):
        raw = _raw()
        raw.json.side_effect = ValueError("Expecting value")
        with pytest.raises(DecodeError) as exc_info:
            CurlResponse(raw, _URL).json()
        assert exc_info.value.url == _URL


@pytest.mark.unit
class TestCurlTransportOpen:
    def test_each_open_builds_a_separate_session(self, session_cls):
        transport = CurlTransport()
        first = transport.open(_PROFILE)
        second = transport.open(_PROFILE)
        assert session_cls.call_count == 2
        assert first is not second
        assert first._session is not second._session

    def test_session_gets_the_profile_headers_cookies_and_timeout(self, session_cls):
        CurlTransport().open(_PROFILE)
        kwargs = session_cls.call_args.kwargs
        assert kwargs["headers"] == {"Referer": "https://shinden.pl/"}
        assert kwargs["cookies"] == {"_rnd": "1"}
        assert kwargs["timeout"] == 12.5
        assert kwargs["impersonate"] == "chrome"
        assert kwargs["allow_redirects"] is True

    def test_profiles_do_not_share_headers_or_cookies(self, session_cls):
        transport = CurlTransport()
        transport.open(_PROFILE)
        transport.open(EXTRACTOR_PROFILE)
        shinden_kwargs, extractor_kwargs = (call.kwargs for call in session_cls.call_args_list)
        assert extractor_kwargs["headers"] == {}
        assert extractor_kwargs["cookies"] == {}
        assert shinden_kwargs["cookies"] == {"_rnd": "1"}

    def test_session_gets_copies_not_the_profile_mappings(self, session_cls):
        CurlTransport().open(_PROFILE)
        kwargs = session_cls.call_args.kwargs
        assert kwargs["headers"] is not _PROFILE.headers
        assert kwargs["cookies"] is not _PROFILE.cookies


@pytest.mark.unit
class TestCurlSession:
    def test_get_forwards_only_the_given_arguments(self, opened):
        session, raw_session = opened
        raw_session.get.return_value = _raw(text="ok")
        response = session.get(_URL, params={"strona": 2})
        raw_session.get.assert_called_once_with(_URL, params={"strona": 2})
        assert isinstance(response, CurlResponse)
        assert (response.text, response.url) == ("ok", _URL)

    def test_post_forwards_headers_json_and_data(self, opened):
        session, raw_session = opened
        raw_session.post.return_value = _raw()
        session.post(_URL, headers={"X": "1"}, json={"a": 1})
        raw_session.post.assert_called_once_with(_URL, headers={"X": "1"}, json={"a": 1})
        session.post(_URL, data="k=v")
        assert raw_session.post.call_args.kwargs == {"data": "k=v"}

    def test_context_manager_closes_the_curl_session(self, session_cls):
        with CurlSession(_PROFILE) as session:
            raw_session = session._session
            raw_session.close.assert_not_called()
        raw_session.close.assert_called_once_with()
