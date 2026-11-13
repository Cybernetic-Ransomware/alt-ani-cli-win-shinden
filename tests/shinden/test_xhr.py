"""Episode XHR context: token and endpoint validation, api4 fallback eligibility, forced backend, secrecy."""

from unittest.mock import MagicMock

import pytest

from alt_ani_core.config import SHINDEN_XHR_BASE
from alt_ani_core.errors import ParseError, ShindenError
from alt_ani_core.shinden.episode import episode_path, episode_url_from_path
from alt_ani_core.shinden.xhr import BACKEND_API4, BACKEND_NEW, load_episode, parse_backend, read_episode

from .conftest import EPISODE_URL, TEST_TOKEN, TRAP_ID

_CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"


def _read(html: str, backend: str = BACKEND_NEW):
    return read_episode(html, EPISODE_URL, backend=backend)


@pytest.mark.unit
class TestContext:
    def test_valid_page_uses_new_backend(self, episode_page):
        players, xhr = _read(episode_page())
        assert xhr.backend == BACKEND_NEW
        assert xhr.context is not None
        assert xhr.context.endpoint == SHINDEN_XHR_BASE
        assert xhr.context.auth == TEST_TOKEN
        assert xhr.fallback_reason is None
        assert xhr.forced is False
        assert xhr.player_ids == frozenset(p.online_id for p in players)
        assert xhr.trap_id == TRAP_ID
        assert TRAP_ID not in xhr.player_ids

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            (None, "auth_missing"),
            ("", "auth_missing"),
            ("short", "auth_malformed"),
            ("abc%3Ddefghijklmnopqrstu", "auth_malformed"),
            ("abc def ghijklmnopqrstuvw", "auth_malformed"),
            ("A" * 4097, "auth_malformed"),
        ],
        ids=["absent", "empty", "too-short", "pre-encoded", "whitespace", "too-long"],
    )
    def test_bad_token_falls_back_to_api4(self, episode_page, token, reason):
        players, xhr = _read(episode_page(token=token))
        assert players
        assert (xhr.backend, xhr.context, xhr.fallback_reason, xhr.forced) == (BACKEND_API4, None, reason, False)

    def test_missing_endpoint_falls_back_to_api4(self, episode_page):
        _, xhr = _read(episode_page(xhr=None))
        assert (xhr.backend, xhr.fallback_reason) == (BACKEND_API4, "endpoint_missing")

    @pytest.mark.parametrize(
        "endpoint",
        [
            "//evil.example/xhr",
            "https://shinden.pl.evil.example/xhr",
            "https://evil.example/shinden.pl/xhr",
            "http://shinden.pl/xhr",
            "https://user@shinden.pl/xhr",
            "//@shinden.pl/xhr",
            "https://shinden.pl:8443/xhr",
            "https://shinden.pl/xhr?next=1",
            "https://shinden.pl/xhr#frag",
            "https://shinden.pl/api/xhr",
            "/xhr",
            "shinden.pl/xhr",
            "//shinden.pl\\\\@evil.example/xhr",
            "javascript:alert(1)",
        ],
    )
    def test_endpoint_off_the_allowlist_is_never_used(self, episode_page, endpoint):
        _, xhr = _read(episode_page(xhr=endpoint))
        assert (xhr.backend, xhr.context, xhr.fallback_reason) == (BACKEND_API4, None, "endpoint_rejected")

    @pytest.mark.parametrize("endpoint", ["//shinden.pl/xhr", "https://shinden.pl/xhr", "//SHINDEN.pl/xhr/"])
    def test_allowed_endpoint_forms_map_to_the_fixed_base(self, episode_page, endpoint):
        _, xhr = _read(episode_page(xhr=endpoint))
        assert xhr.context is not None
        assert xhr.context.endpoint == SHINDEN_XHR_BASE

    def test_html_entities_in_the_token_are_decoded_once(self, episode_page):
        _, xhr = _read(episode_page(token="VGVzdFRva2VuRm9yVW5pdFRlc3RzT25seQ&#61;&#61;"))
        assert xhr.context is not None
        assert xhr.context.auth == "VGVzdFRva2VuRm9yVW5pdFRlc3RzT25seQ=="


@pytest.mark.unit
class TestNoFallbackOnProtectionPages:
    """Only a valid page with a valid player list may fall back; anything else raises before the token is looked at."""

    def test_challenge_page_raises(self):
        with pytest.raises(ParseError):
            _read(_CHALLENGE)

    def test_age_gate_page_raises(self, episode_page):
        html = episode_page(token=None).replace("<body>", "<body><p>Musisz mieć ukończone 18 lat</p>", 1)
        with pytest.raises(ShindenError):
            _read(html)

    def test_page_without_player_data_raises(self):
        with pytest.raises(ParseError):
            _read("<html><head><title>Odcinek</title></head><body><p>nothing here</p></body></html>")

    def test_truncated_player_json_raises(self, episode_page):
        html = episode_page(token=None)
        cut = html.index('"players": [') + 40
        with pytest.raises(ParseError):
            _read(html[:cut])


@pytest.mark.unit
class TestPageContract:
    @pytest.mark.parametrize("backend", [BACKEND_NEW, BACKEND_API4])
    def test_missing_honeypot_declaration_raises_on_every_backend(self, episode_page, backend):
        html = episode_page().replace(f'"pulapka": {TRAP_ID}', '"renamed": 1', 1)
        with pytest.raises(ParseError, match="honeypot"):
            _read(html, backend=backend)

    def test_empty_player_data_with_visible_rows_is_drift_not_a_missing_token(self, episode_page):
        html = episode_page(token=None)
        start = html.index('"players": [') + len('"players": ')
        end = html.index('], "episodeId"') + 1
        with pytest.raises(ParseError, match="player rows are visible"):
            _read(html[:start] + "[]" + html[end:])

    def test_player_json_is_parsed_once(self, episode_page, monkeypatch):
        from alt_ani_core.shinden import episode

        calls = []
        original = episode._player_data
        monkeypatch.setattr(episode, "_player_data", lambda tree: calls.append(1) or original(tree))
        _read(episode_page())
        assert len(calls) == 1


@pytest.mark.unit
class TestForcedApi4:
    def test_forced_api4_skips_the_token_but_keeps_page_validation(self, episode_page):
        players, xhr = _read(episode_page(), backend=BACKEND_API4)
        assert (xhr.backend, xhr.context, xhr.forced, xhr.fallback_reason) == (BACKEND_API4, None, True, None)
        assert xhr.player_ids == frozenset(p.online_id for p in players)
        assert xhr.trap_id == TRAP_ID

    def test_forced_api4_still_rejects_a_challenge_page(self):
        with pytest.raises(ParseError):
            _read(_CHALLENGE, backend=BACKEND_API4)


@pytest.mark.unit
class TestIsolationAndSecrecy:
    def test_each_page_read_builds_its_own_state(self, episode_page):
        _, first = _read(episode_page(token="Rmlyc3RFcGlzb2RlVG9rZW4="))
        _, second = _read(episode_page(token="U2Vjb25kRXBpc29kZVRva2Vu"))
        assert first is not second
        assert first.context is not None and second.context is not None
        assert first.context.auth != second.context.auth

    def test_token_never_appears_in_repr_or_str(self, episode_page):
        _, xhr = _read(episode_page())
        assert xhr.context is not None
        for text in (repr(xhr), str(xhr), repr(xhr.context), str(xhr.context), f"{xhr!r}"):
            assert TEST_TOKEN not in text

    def test_load_episode_fetches_the_given_page_once(self, episode_page):
        client = MagicMock()
        client.get.return_value = MagicMock(text=episode_page(), raise_for_status=MagicMock())
        players, xhr = load_episode(client, EPISODE_URL, backend=BACKEND_NEW)
        client.get.assert_called_once_with(EPISODE_URL)
        assert players and xhr.episode_url == EPISODE_URL


@pytest.mark.unit
class TestParseBackend:
    @pytest.mark.parametrize(("value", "expected"), [("new", BACKEND_NEW), ("api4", BACKEND_API4), (" API4 ", BACKEND_API4)])
    def test_known_values(self, value, expected):
        assert parse_backend(value) == expected

    @pytest.mark.parametrize("value", ["", "auto", "new_xhr", "legacy"])
    def test_unknown_value_is_rejected(self, value):
        with pytest.raises(ValueError, match="ALT_ANI_CLI_XHR_BACKEND"):
            parse_backend(value)


@pytest.mark.unit
class TestEpisodePath:
    def test_plain_episode_url_gives_its_path(self):
        assert episode_path(EPISODE_URL) == "/episode/61064-sousou-no-frieren/view/227851"

    @pytest.mark.parametrize(
        "url",
        [
            "http://shinden.pl/episode/1-x/view/2",
            "https://evil.example/episode/1-x/view/2",
            "https://shinden.pl/episode/1-x/view/2?auth=secret",
            "https://shinden.pl/episode/1-x/view/2#x",
            "https://user@shinden.pl/episode/1-x/view/2",
            "https://shinden.pl:444/episode/1-x/view/2",
            "https://shinden.pl/series/1-x",
            "https://shinden.pl/episode/1-x/view/2/../../xhr",
        ],
    )
    def test_anything_else_is_not_logged(self, url):
        assert episode_path(url) is None

    def test_path_round_trips_to_a_shinden_url(self):
        assert episode_url_from_path("/episode/1-x/view/2") == "https://shinden.pl/episode/1-x/view/2"

    @pytest.mark.parametrize("path", ["//evil.example/episode/1-x/view/2", "/episode/1-x/view/2?a=1", "/xhr/1/player_load"])
    def test_untrusted_path_is_not_rebuilt(self, path):
        assert episode_url_from_path(path) is None
