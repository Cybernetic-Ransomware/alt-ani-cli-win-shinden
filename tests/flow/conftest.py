from pathlib import Path

import pytest

from .support import EP1, PLAYER, PLAYER2, SERIES_REF, FakeShinden, episode_html, make_state

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "shinden"
_ISLAND = '<div class="ep-players__island" data-sh-module="episode-players">'


@pytest.fixture
def shinden_html():
    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def token_page(shinden_html):
    """episode_players.html with a test token injected; fixture files never hold data-player-auth themselves."""

    def _make(token: str) -> str:
        return shinden_html("episode_players.html").replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{token}">', 1)

    return _make


@pytest.fixture
def shinden(transport) -> FakeShinden:
    return FakeShinden(transport)


@pytest.fixture
def open_state(make_engine, shinden):
    """A state past EPISODE_DISPATCH on a real engine: the first target's page is served and opened as its session."""

    def _open(players=(PLAYER, PLAYER2), *, targets=(EP1,), options=None, xhr_backend="api4", **fields):
        engine = make_engine(xhr_backend=xhr_backend)
        for ep in targets:
            shinden.episode(ep, episode_html(players))
        session = engine.open_episode(targets[0])
        defaults = dict(ref=SERIES_REF, targets=list(targets), ep_idx=0, players=list(session.sources), session=session)
        return make_state(options=options, engine=engine, **(defaults | fields))

    return _open
