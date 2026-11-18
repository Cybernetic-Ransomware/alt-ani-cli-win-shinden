import re
from unittest.mock import MagicMock

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import OperationCancelled, ParseError, PlayerNotFoundError, ShindenError
from alt_ani_core.models import EmbedURL, PlayerEntry
from alt_ani_core.providers.api import StaticEmbeds, url_matches

_HOSTS = frozenset({"site.example", "www.site.example"})
_PATH = re.compile(r"/anime/[^/]+(?:/|$)")
_EMBED = EmbedURL(url="https://host.example/e/1", referer="https://site.example/")


def _player(online_id: str) -> PlayerEntry:
    return PlayerEntry(online_id=online_id, player="Host", lang_audio="jp", lang_subs="pl")


_PLAYERS = [_player("1")]


@pytest.mark.unit
class TestUrlMatches:
    @pytest.mark.parametrize(
        "url",
        [
            "https://site.example/anime/elfen-lied",
            "https://site.example/anime/elfen-lied/10-5/",
            "http://www.site.example/anime/x?p=1",
            "https://SITE.example/anime/x",
        ],
    )
    def test_accepts_known_host_and_path(self, url):
        assert url_matches(url, hosts=_HOSTS, path=_PATH)

    @pytest.mark.parametrize(
        "url",
        [
            "site.example/anime/x",
            "ftp://site.example/anime/x",
            "javascript://site.example/anime/x",
            "https://evilsite.example/anime/x",
            "https://site.example.evil.example/anime/x",
            "https://evil.example/?u=https://site.example/anime/x",
            "https://evil.example/site.example/anime/x",
            "https://user@site.example/anime/x",
            "https://user:pw@site.example/anime/x",
            "https://site.example:8443/anime/x",
            "https://site.example:99999/anime/x",
            "https://site.example/manga/x",
            "https://site.example/",
            "https://site.example/anime/",
            "https://site.example/anime/x/../../admin",
            "https://site.example/anime/x/%2e%2e/admin",
            "https://site.example/anime/a b",
            "https://site.example\\@evil.example/anime/x",
            "https://[::1/anime/x",
            "",
        ],
    )
    def test_rejects_anything_else(self, url):
        assert not url_matches(url, hosts=_HOSTS, path=_PATH)


@pytest.mark.unit
class TestStaticEmbeds:
    def test_returns_the_listed_embed_without_a_request(self):
        client = MagicMock()

        embed = StaticEmbeds(_PLAYERS, {"1": _EMBED}).embed(client, "1", cancel=CancelToken())

        assert embed == _EMBED
        assert client.mock_calls == []

    def test_has_no_antibot_phase_no_diagnostics_and_never_stops_an_operation(self):
        source = StaticEmbeds(_PLAYERS, {"1": _EMBED})
        source.begin_operation()

        assert source.antibot_phase is False
        assert source.diag() == {}
        assert source.stops_operation(ShindenError("x")) is False

    def test_cancelled_token_raises_before_lookup(self):
        token = CancelToken()
        token.cancel()

        with pytest.raises(OperationCancelled):
            StaticEmbeds(_PLAYERS, {"1": _EMBED}).embed(MagicMock(), "1", cancel=token)

    def test_unlisted_player_is_a_classified_error(self):
        with pytest.raises(PlayerNotFoundError) as exc_info:
            StaticEmbeds(_PLAYERS, {"1": _EMBED}).embed(MagicMock(), "2", cancel=CancelToken())

        assert exc_info.value.code == "episode.player_not_listed"

    @pytest.mark.parametrize(
        ("players", "embeds", "params"),
        [
            ([_player("1"), _player("2")], {"1": _EMBED}, {"missing": ["2"], "extra": [], "duplicate": []}),
            ([_player("1")], {"1": _EMBED, "2": _EMBED}, {"missing": [], "extra": ["2"], "duplicate": []}),
            ([_player("1"), _player("1")], {"1": _EMBED}, {"missing": [], "extra": [], "duplicate": ["1"]}),
        ],
        ids=["missing_embed", "extra_embed", "duplicate_player"],
    )
    def test_players_and_embeds_that_disagree_are_a_parse_error(self, players, embeds, params):
        with pytest.raises(ParseError) as exc_info:
            StaticEmbeds(players, embeds)

        assert exc_info.value.code == "episode.embeds_mismatch"
        assert dict(exc_info.value.params) == params

    def test_later_changes_to_the_source_mapping_are_not_seen(self):
        embeds = {"1": _EMBED}
        source = StaticEmbeds(_PLAYERS, embeds)
        embeds.clear()

        assert source.embed(MagicMock(), "1", cancel=CancelToken()) == _EMBED
