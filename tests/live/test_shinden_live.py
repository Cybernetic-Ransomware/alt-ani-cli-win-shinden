"""Opt-in live checks (``just test-live``): structural asserts only, ~4 requests, CF cache never written."""

import json

import pytest
from selectolax.parser import HTMLParser

from alt_ani_cli.config import FLARESOLVERR_URL
from alt_ani_cli.shinden import episode, http, search, series

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not FLARESOLVERR_URL, reason="ALT_ANI_CLI_FLARESOLVERR_URL is not set"),
]


@pytest.fixture(scope="module")
def client():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(http, "_save_cached", lambda *a, **k: None)
        session = http.make_client()
    yield session
    session.close()


@pytest.fixture(scope="module")
def first_series_hit(client):
    hits = search.search_series(client, "frieren")
    assert hits, "search returned no hits for a popular title"
    return next(h for h in hits if "/series/" in h.url)


@pytest.fixture(scope="module")
def listing(client, first_series_hit):
    return series.list_episodes(client, series.parse_series_url(first_series_hit.url))


def test_search_hits_are_well_formed(first_series_hit):
    assert first_series_hit.id.isdigit()
    assert first_series_hit.title
    assert first_series_hit.series_type


def test_episode_list_is_complete_and_ascending(listing):
    assert listing.complete
    numbers = [ep.number for ep in listing.episodes]
    assert numbers and numbers == sorted(numbers)
    assert all("/episode/" in ep.url for ep in listing.episodes)


def test_players_parse_and_exclude_honeypot(client, listing):
    resp = client.get(listing.episodes[-1].url)
    resp.raise_for_status()
    players = episode.parse_players(resp.text)
    assert players
    script = HTMLParser(resp.text).css_first("script#episode-players-data")
    trap = json.loads(script.text()).get("pulapka")
    assert str(trap) not in {p.online_id for p in players}
