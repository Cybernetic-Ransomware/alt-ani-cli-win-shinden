"""T3b, opt-in: full live Shinden pages, fetched once, parse to the same DTOs on the selectolax and stdlib backends."""

import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from alt_ani_cli.paths import flaresolverr_url
from alt_ani_core import dom
from alt_ani_core.config import AccessConfig
from alt_ani_core.dom import selectolax_backend, stdlib_backend
from alt_ani_core.shinden import metadata, search, series, xhr
from alt_ani_core.shinden.utils import ParseTally
from alt_ani_core.transport import clearance
from alt_ani_core.transport.api import HttpStatusError
from alt_ani_core.transport.curl import CurlTransport

FLARESOLVERR_URL = flaresolverr_url()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not FLARESOLVERR_URL, reason="ALT_ANI_CLI_FLARESOLVERR_URL is not set"),
]

_QUERIES = ("frieren", "one piece", "spy x family")
_BACKENDS = {"selectolax": selectolax_backend.parse, "stdlib": stdlib_backend.parse}


@dataclass(frozen=True)
class _Recorded:
    status_code: int
    text: str
    url: str

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpStatusError(self.status_code, url=self.url)


class _Recorder:
    """Records live answers on the first pass and replays them afterwards, so both backends see the same bytes."""

    def __init__(self, live) -> None:
        self._live = live
        self.answers: dict[tuple[str, tuple], _Recorded] = {}

    def get(self, url, *, headers=None, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        if key not in self.answers:
            if self._live is None:
                raise AssertionError(f"not recorded: {url} {params}")
            resp = self._live.get(url, headers=headers, params=params)
            self.answers[key] = _Recorded(resp.status_code, resp.text, url)
        return self.answers[key]


def _replay(recorder: _Recorder) -> _Recorder:
    copy = _Recorder(None)
    copy.answers = recorder.answers
    return copy


def _parse_everything(client, query: str) -> dict:
    hits = search.search_series(client, query).hits
    hit = next(h for h in hits if "/series/" in h.url)
    ref = series.parse_series_url(hit.url)
    listing = series.list_episodes(client, ref)
    meta = metadata.fetch_series_metadata(client, ref)
    episode_url = listing.episodes[-1].url
    players, page_xhr = xhr.read_episode(client.get(episode_url).text, episode_url, backend=xhr.BACKEND_NEW)
    return {
        "hits": hits,
        "listing": listing,
        "metadata": meta,
        "players": players,
        "xhr": (page_xhr.backend, page_xhr.fallback_reason, page_xhr.context is not None),
    }


@pytest.fixture(scope="module")
def recorded():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(clearance, "save_cached", lambda *a, **k: None)
        found = clearance.get_clearance(
            AccessConfig(flaresolverr_url=FLARESOLVERR_URL), Path(tempfile.mkdtemp(prefix="t3b-clearance-"))
        )
    with CurlTransport().open(clearance.shinden_profile(found)) as live:
        recorders = {}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(dom, "_backend", selectolax_backend.parse)
            for query in _QUERIES:
                recorders[query] = _Recorder(live)
                _parse_everything(recorders[query], query)
    return recorders


@pytest.mark.parametrize("query", _QUERIES)
def test_both_backends_give_the_same_dtos(recorded, query, monkeypatch):
    results = {}
    for name, parse in _BACKENDS.items():
        monkeypatch.setattr(dom, "_backend", parse)
        results[name] = _parse_everything(_replay(recorded[query]), query)
    for part in results["selectolax"]:
        assert results["stdlib"][part] == results["selectolax"][part], part
    assert results["stdlib"]["listing"].complete


def test_episode_list_parse_time(recorded, monkeypatch):
    """Desktop numbers for comparison with the device; the 300 ms p95 limit applies to the phone only."""
    pages = [a.text for (url, _), a in recorded["one piece"].answers.items() if url.endswith("/episodes")]
    assert pages
    for name, parse in _BACKENDS.items():
        monkeypatch.setattr(dom, "_backend", parse)
        times = []
        for _ in range(5):
            for html in pages:
                start = time.perf_counter()
                series._parse_page(html, ParseTally("episodes"))
                times.append((time.perf_counter() - start) * 1000)
        p95 = statistics.quantiles(times, n=20)[-1]
        print(f"T3B_DESKTOP backend={name} pages={len(pages)} bytes_max={max(map(len, pages))} p95_ms={p95:.1f}")
