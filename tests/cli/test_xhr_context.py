"""Noninteractive XHR context: one episode page per context, api4 fallback per episode, controlled stops."""

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

from alt_ani_cli import config, diagnostics
from alt_ani_cli.cli import _run_noninteractive, main
from alt_ani_cli.config import SHINDEN_API_BASE, SHINDEN_XHR_BASE
from alt_ani_cli.errors import ShindenXhrError
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EpisodeListing, EpisodeRow, SeriesRef
from alt_ani_cli.player.runner import PlayResult
from alt_ani_cli.shinden import episode as shinden_episode
from alt_ani_cli.shinden.api import XhrOutcome, xhr_outcome

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "shinden" / "episode_players.html"
_ISLAND = '<div class="ep-players__island" data-sh-module="episode-players">'
_TRAP = "2045542847"
_REF = SeriesRef(id="61064", slug="sousou-no-frieren", title="Frieren", url="https://shinden.pl/series/61064-sousou-no-frieren")
_EP1 = EpisodeRow(number=1.0, title="Ep 1", url="https://shinden.pl/episode/61064-sousou-no-frieren/view/1001")
_EP2 = EpisodeRow(number=2.0, title="Ep 2", url="https://shinden.pl/episode/61064-sousou-no-frieren/view/1002")
_TOKEN_1 = "RXBpc29kZU9uZVRva2VuMDE="
_TOKEN_2 = "RXBpc29kZVR3b1Rva2VuMDI="
_IFRAME = '<iframe src="https://host.example/e/abc"></iframe>'
_INVALID_TOKEN = '{"error": {"code": "invalid_token", "message": "expired"}}'


def _page(token: str | None, trap: str = _TRAP) -> str:
    html = _FIXTURE.read_text(encoding="utf-8")
    if token is not None:
        html = html.replace(_ISLAND, _ISLAND[:-1] + f' data-player-auth="{token}">', 1)
    return html.replace(f'"pulapka": {_TRAP}', f'"pulapka": {trap}', 1)


def _first_player(html: str) -> str:
    return shinden_episode.sort_players(shinden_episode.parse_players(html))[0].online_id


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        query=[],
        url=_REF.url,
        resume=False,
        download=False,
        delete_history=False,
        episode="1-2",
        quality=None,
        select_nth=None,
        vlc=False,
        no_detach=False,
        debug=False,
        player_name=None,
        lang=None,
        subs=None,
        allow_fallback=False,
        show_sources=False,
        cookies_file=None,
        cookies_browser=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _client(pages: dict[str, list[str]], xhr):
    """pages: episode url -> successive page bodies; xhr(url) -> (status, body) for player_load/player_show."""

    def get(url, **_kw):
        if url in pages:
            return MagicMock(status_code=200, text=pages[url].pop(0), headers={}, raise_for_status=MagicMock())
        status, body = xhr(url)
        return MagicMock(status_code=status, text=body, headers={})

    client = MagicMock()
    client.get.side_effect = get
    return client


def _ok(url: str) -> tuple[int, str]:
    return (200, "5") if "/player_load" in url else (200, _IFRAME)


def _run(client, **arg_overrides):
    with (
        patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
        patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=EpisodeListing(_REF, [_EP1, _EP2])),
        patch("alt_ani_cli.shinden.api.time.sleep"),
        patch("alt_ani_cli.cli.extract.resolve", return_value=Stream(url="https://cdn.example/v.mp4")),
        patch("alt_ani_cli.cli.player_runner.play", return_value=PlayResult(0, 0.0)) as mock_play,
        patch("alt_ani_cli.cli.history.upsert"),
        patch("alt_ani_cli.cli.progress") as mock_progress,
    ):
        _run_noninteractive(_args(**arg_overrides), client)
    return mock_play, mock_progress


def _xhr_calls(client) -> list[str]:
    return [c.args[0] for c in client.get.call_args_list if "/xhr/" in c.args[0]]


@pytest.fixture(autouse=True)
def _new_backend(monkeypatch):
    monkeypatch.setattr(config, "XHR_BACKEND", "new")


@pytest.fixture
def diag_events(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(diagnostics, "_emit", lambda event, **fields: events.append((event, fields)))
    return events


@pytest.mark.unit
class TestEpisodeIsolation:
    def test_each_episode_uses_only_its_own_token(self):
        client = _client({_EP1.url: [_page(_TOKEN_1)], _EP2.url: [_page(_TOKEN_2)]}, _ok)
        mock_play, _ = _run(client)
        assert mock_play.call_count == 2
        urls = [c.args[0] for c in client.get.call_args_list]
        ep2_at = urls.index(_EP2.url)
        first, second = urls[:ep2_at], urls[ep2_at:]
        enc1, enc2 = quote(_TOKEN_1, safe=""), quote(_TOKEN_2, safe="")
        assert all(enc1 in u and enc2 not in u for u in first if "/xhr/" in u)
        assert all(enc2 in u and enc1 not in u for u in second if "/xhr/" in u)
        assert all(u.startswith(SHINDEN_XHR_BASE) for u in _xhr_calls(client))

    def test_fallback_on_one_episode_does_not_carry_over(self, diag_events):
        client = _client({_EP1.url: [_page(None)], _EP2.url: [_page(_TOKEN_2)]}, _ok)
        _run(client)
        urls = [c.args[0] for c in client.get.call_args_list]
        ep2_at = urls.index(_EP2.url)
        assert all(u.startswith(SHINDEN_API_BASE) for u in urls[:ep2_at] if "/xhr/" in u)
        assert all(u.startswith(SHINDEN_XHR_BASE) for u in urls[ep2_at:] if "/xhr/" in u)
        fallbacks = [f for e, f in diag_events if e == "shinden_xhr_context" and f["action"] == "fallback"]
        assert fallbacks == [{"action": "fallback", "backend": "api4", "reason": "auth_missing", "online_id": None}]

    def test_fallback_is_decided_once_per_episode_not_per_player(self, diag_events):
        client = _client({_EP1.url: [_page(None)], _EP2.url: [_page(None)]}, lambda url: (404, ""))
        _, mock_progress = _run(client)
        calls = _xhr_calls(client)
        assert len(calls) > 2
        assert all(u.startswith(SHINDEN_API_BASE) for u in calls)
        fallbacks = [f for e, f in diag_events if e == "shinden_xhr_context" and f["action"] == "fallback"]
        assert len(fallbacks) == 2
        warns = [c.args[0] for c in mock_progress.warn.call_args_list if "api4" in c.args[0]]
        assert len(warns) == 2


@pytest.mark.unit
class TestStops:
    def test_player_list_change_after_refresh_stops_with_a_message(self, capsys):
        first = _page(_TOKEN_1)
        chosen = _first_player(first)
        refreshed = _page(_TOKEN_2, trap=chosen)
        loads = iter([(403, _INVALID_TOKEN)])

        def xhr(url):
            return next(loads, (200, "5")) if "/player_load" in url else (200, _IFRAME)

        client = _client({_EP1.url: [first, refreshed], _EP2.url: [_page(_TOKEN_2)]}, xhr)
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", _REF.url, "-e", "1-2"]),
            patch("alt_ani_cli.cli.shinden_http.make_client", return_value=client),
            patch("alt_ani_cli.cli.shinden_series.parse_series_url", return_value=_REF),
            patch("alt_ani_cli.cli.shinden_series.list_episodes", return_value=EpisodeListing(_REF, [_EP1, _EP2])),
            patch("alt_ani_cli.shinden.api.time.sleep"),
            patch("alt_ani_cli.cli.player_runner.play") as mock_play,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        assert len(_xhr_calls(client)) == 1
        assert _EP2.url not in [c.args[0] for c in client.get.call_args_list]
        mock_play.assert_not_called()
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "Traceback" not in out
        assert _TOKEN_1 not in out and _TOKEN_2 not in out

    def test_players_changed_is_a_stop_for_the_auto_walk(self):
        first = _page(_TOKEN_1)
        refreshed = _page(_TOKEN_2, trap=_first_player(first))
        loads = iter([(403, _INVALID_TOKEN)])
        client = _client({_EP1.url: [first, refreshed]}, lambda url: next(loads, (200, "5")))
        with pytest.raises(ShindenXhrError) as exc_info:
            _run(client, episode="1")
        assert xhr_outcome(exc_info.value) is XhrOutcome.PLAYERS_CHANGED

    def test_invalid_backend_value_exits_before_any_request(self, monkeypatch, capsys):
        monkeypatch.setattr(config, "XHR_BACKEND", "legacy")
        with (
            patch("sys.argv", ["alt-ani-cli", "--url", _REF.url]),
            patch("alt_ani_cli.cli.shinden_http.make_client") as mock_client,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
        mock_client.assert_not_called()
        captured = capsys.readouterr()
        assert "ALT_ANI_CLI_XHR_BACKEND" in captured.out + captured.err


@pytest.mark.unit
class TestForcedApi4:
    def test_forced_api4_ignores_the_token_and_records_the_choice(self, monkeypatch, diag_events):
        monkeypatch.setattr(config, "XHR_BACKEND", "api4")
        client = _client({_EP1.url: [_page(_TOKEN_1)], _EP2.url: [_page(_TOKEN_2)]}, _ok)
        _run(client)
        assert all(u.startswith(SHINDEN_API_BASE) for u in _xhr_calls(client))
        assert not any(quote(_TOKEN_1, safe="") in u for u in _xhr_calls(client))
        forced = [f for e, f in diag_events if e == "shinden_xhr_context"]
        assert [f["action"] for f in forced] == ["forced", "forced"]
        results = [f for e, f in diag_events if e == "resolve_result"]
        assert {f["xhr_backend"] for f in results} == {"api4"}
