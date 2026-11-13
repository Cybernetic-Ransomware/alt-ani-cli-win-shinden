import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alt_ani_core import diagnostics
from alt_ani_core.services import episode_session
from alt_ani_core.testing import FakeTransport, make_stream

from .support import EPISODE, EPISODE_URL, TEST_TOKEN, route_xhr

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "shinden"
_ISLAND = '<div class="ep-players__island" data-sh-module="episode-players">'


class TrackingTransport(FakeTransport):
    """Every session also records the threads that sent requests through it."""

    def open(self, profile):
        session = super().open(profile)
        session.threads = set()
        inner_get = session.get

        def get(url, **kwargs):
            session.threads.add(threading.get_ident())
            return inner_get(url, **kwargs)

        session.get = get
        return session


@pytest.fixture
def transport() -> TrackingTransport:
    return TrackingTransport()


@pytest.fixture
def shinden_html():
    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def episode_page(shinden_html):
    """episode_players.html with a test token injected; fixture files never hold data-player-auth themselves."""

    def _make(token: str | None = TEST_TOKEN, xhr: str | None = "//shinden.pl/xhr") -> str:
        html = shinden_html("episode_players.html")
        if token is not None:
            html = html.replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{token}">', 1)
        if xhr is None:
            html = html.replace('"xhr": "//shinden.pl/xhr", ', "", 1)
        else:
            html = html.replace('"xhr": "//shinden.pl/xhr"', f'"xhr": "{xhr}"', 1)
        return html

    return _make


@pytest.fixture
def diag_events(monkeypatch):
    """Diagnostics as (event, fields) instead of the log file."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(diagnostics, "_emit", lambda event, **fields: events.append((event, fields)))
    return events


@pytest.fixture
def diag(monkeypatch):
    """The diagnostics calls the session makes, as mocks."""
    names = ("resolve_result", "host_health", "health_defer", "player_selected", "health_reorder")
    mocks = SimpleNamespace(**{name: MagicMock(name=name) for name in names})
    for name in names:
        monkeypatch.setattr(diagnostics, name, getattr(mocks, name))
    return mocks


@pytest.fixture
def embed_seam(monkeypatch):
    mock = MagicMock(name="resolve_embed")
    monkeypatch.setattr(episode_session.shinden_api, "resolve_embed", mock)
    return mock


@pytest.fixture
def extract_seam(monkeypatch):
    mock = MagicMock(name="extract.resolve", return_value=make_stream())
    monkeypatch.setattr(episode_session.extract, "resolve", mock)
    return mock


@pytest.fixture
def opened(make_engine, transport, episode_page):
    """An engine plus a session on the fixture episode; XHR answers at once with a per-player embed host."""

    def _open(page=None, **config):
        transport.route(EPISODE_URL, page if page is not None else episode_page())
        route_xhr(transport)
        engine = make_engine(**config)
        return engine, engine.open_episode(EPISODE)

    return _open
