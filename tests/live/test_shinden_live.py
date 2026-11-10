"""Opt-in live checks (``just test-live``): structural asserts only, ~8 requests, CF cache never written."""

import json
import re

import pytest
from selectolax.parser import HTMLParser

from alt_ani_cli.config import FLARESOLVERR_URL, GUEST_AUTH, SHINDEN_API_BASE, SHINDEN_XHR_BASE
from alt_ani_cli.redaction import redact_text
from alt_ani_cli.shinden import api, episode, http, metadata, search, series, xhr

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


def test_series_metadata_is_parsed(client, first_series_hit):
    meta = metadata.fetch_series_metadata(client, series.parse_series_url(first_series_hit.url))
    assert meta.air_date_sort is not None
    assert meta.description
    assert meta.tags
    assert all(r.id.isdigit() and ("/series/" in r.url or "/titles/" in r.url) for r in meta.related)


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


def test_player_load_body_is_whole_seconds(client, listing):
    # contract gate for api.resolve_embed's wait parser; one player_load, no player_show
    resp = client.get(listing.episodes[-1].url)
    resp.raise_for_status()
    online_id = episode.parse_players(resp.text)[0].online_id
    load = client.get(f"{SHINDEN_API_BASE}/xhr/{online_id}/player_load?auth={GUEST_AUTH}")
    body = load.text.strip()
    shape = f"status={load.status_code} content_type={load.headers.get('content-type')!r} len={len(load.text)} head={redact_text(body[:40])!r}"
    assert load.status_code == 200, shape
    assert re.fullmatch(r"\d{1,3}", body) or isinstance(json.loads(body), int | float), shape


def test_new_xhr_resolves_an_iframe_with_the_page_token(client, listing):
    players, page_xhr = xhr.load_episode(client, listing.episodes[-1].url, backend=xhr.BACKEND_NEW)
    assert players
    assert page_xhr.backend == xhr.BACKEND_NEW, f"fallback_reason={page_xhr.fallback_reason}"
    assert page_xhr.context is not None
    assert page_xhr.context.endpoint == SHINDEN_XHR_BASE
    page_xhr.begin_operation()
    embed = api.resolve_embed(client, players[0].online_id, xhr=page_xhr)
    assert embed.url.startswith("https://")
    assert page_xhr.refresh_used is False
