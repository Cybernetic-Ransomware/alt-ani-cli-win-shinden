from unittest.mock import MagicMock

import pytest

from alt_ani_core.cancel import NEVER
from alt_ani_core.config import AccessConfig
from alt_ani_core.errors import NoStreamError, ShindenXhrError
from alt_ani_core.events import NULL_SINK
from alt_ani_core.models import EmbedURL
from alt_ani_core.providers.shinden import ShindenEmbeds, ShindenProvider
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.shinden.xhr import BACKEND_API4, EpisodeXhr

_EPISODE_URL = "https://shinden.pl/episode/61064-sousou-no-frieren/view/227851"


def _provider() -> ShindenProvider:
    return ShindenProvider(xhr_backend="new", antibot_delay_sec=3.0, events=NULL_SINK)


def _xhr_error(http_status: int) -> ShindenXhrError:
    return ShindenXhrError(
        "xhr failed", stage="player_load", category="http_error", reason="http_status", http_status=http_status
    )


def _xhr() -> EpisodeXhr:
    return EpisodeXhr(_EPISODE_URL, frozenset({"42"}), None, BACKEND_API4, fallback_reason="auth_missing")


@pytest.mark.unit
class TestOwns:
    @pytest.mark.parametrize(
        "url",
        [
            "https://shinden.pl/series/61064-sousou-no-frieren",
            "https://shinden.pl/series/61064-sousou-no-frieren/episodes",
            "https://shinden.pl/titles/12-one-piece",
            "http://shinden.pl/series/1-fate",
            "https://www.shinden.pl/series/1-fate",
            _EPISODE_URL,
            "http://shinden.pl/episode/1-fate/view/2",
        ],
    )
    def test_series_and_episode_pages_are_shinden(self, url):
        assert _provider().owns(url)

    @pytest.mark.parametrize(
        "url",
        [
            "shinden.pl/series/1-fate",
            "https://evilshinden.pl/series/1-fate",
            "https://shinden.pl.evil.example/series/1-fate",
            "https://evil.example/?next=https://shinden.pl/series/1-fate",
            "https://evil.example/shinden.pl/series/1-fate",
            "https://user@shinden.pl/series/1-fate",
            "https://shinden.pl:444/series/1-fate",
            "https://shinden.pl/manga/57765-sousou-no-frieren",
            "https://shinden.pl/series/fate",
            "https://shinden.pl/xhr/42/player_load",
            "https://shinden.pl/episode/1-x/view/2/../../xhr",
            "https://api4.shinden.pl/series/1-fate",
            "https://anime-odcinki.pl/anime/elfen-lied/1/",
        ],
    )
    def test_anything_else_is_not(self, url):
        assert not _provider().owns(url)


@pytest.mark.unit
class TestShindenProvider:
    def test_invalid_xhr_backend_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            ShindenProvider(xhr_backend="legacy", antibot_delay_sec=0.0, events=NULL_SINK)

    def test_direct_access_profile_has_no_clearance(self, tmp_path):
        profile = _provider().session_profile(AccessConfig(use_flaresolverr=False), tmp_path)

        assert profile.name == "shinden"
        assert "cf_clearance" not in profile.cookies
        assert "_rnd" in profile.cookies

    def test_series_url_parses_as_before(self):
        ref = _provider().series_from_url("https://shinden.pl/titles/12-one-piece/episodes")

        assert (ref.id, ref.slug, ref.url) == ("12", "one-piece", "https://shinden.pl/titles/12-one-piece")


@pytest.mark.unit
class TestShindenEmbeds:
    def test_embed_is_the_player_api_call_with_the_configured_wait(self, monkeypatch):
        resolve = MagicMock(return_value=EmbedURL(url="https://h.example/e/1", referer="https://shinden.pl/"))
        monkeypatch.setattr(shinden_api, "resolve_embed", resolve)
        xhr = _xhr()
        client = MagicMock()

        embed = ShindenEmbeds(xhr, min_sleep_seconds=4.5).embed(client, "42", cancel=NEVER)

        assert embed.url == "https://h.example/e/1"
        resolve.assert_called_once_with(client, "42", xhr=xhr, min_sleep_seconds=4.5, cancel=NEVER)

    def test_begin_operation_resets_the_refresh_budget(self):
        xhr = _xhr()
        xhr.refresh_used = True

        ShindenEmbeds(xhr, min_sleep_seconds=0.0).begin_operation()

        assert xhr.refresh_used is False

    def test_diag_reports_the_xhr_state(self):
        source = ShindenEmbeds(_xhr(), min_sleep_seconds=0.0)

        assert source.antibot_phase is True
        assert source.diag() == {"xhr_backend": "api4", "token_refreshed": False, "xhr_fallback": "auth_missing"}

    @pytest.mark.parametrize(
        ("exc", "stops"),
        [
            (_xhr_error(429), True),
            (_xhr_error(503), True),
            (_xhr_error(404), False),
            (NoStreamError("x"), False),
        ],
        ids=["blocked", "unavailable", "next_player", "not_xhr"],
    )
    def test_only_a_shared_api_failure_stops_the_operation(self, exc, stops):
        assert ShindenEmbeds(_xhr(), min_sleep_seconds=0.0).stops_operation(exc) is stops
