import json
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from alt_ani_cli import paths
from alt_ani_cli.args import CliOptions
from alt_ani_cli.flow.handlers import handle_run_action
from alt_ani_cli.flow.state import FlowState
from alt_ani_core.config import ExtractOptions
from alt_ani_core.errors import DownloadFailedError, ReloadLimitError, ShindenXhrError
from alt_ani_core.models import (
    EmbedURL,
    EpisodeRow,
    FallbackPolicy,
    PlayerEntry,
    PlayerSource,
    Purpose,
    ResolvedStream,
    SeriesHit,
    SeriesRef,
    SourceFilter,
    SourceSelection,
    Stream,
)
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.testing import FakeRequest, FakeResponse, FakeTransport, InMemoryHistory, RecordingSink, make_stream

SERIES_REF = SeriesRef(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
SERIES_HIT = SeriesHit(id="1", slug="fate", title="Fate", url="http://shinden.pl/series/1-fate")
EP1 = EpisodeRow(number=1.0, title="Ep 1", url="http://shinden.pl/ep/1")
EP2 = EpisodeRow(number=2.0, title="Ep 2", url="http://shinden.pl/ep/2")
EP3 = EpisodeRow(number=3.0, title="Ep 3", url="http://shinden.pl/ep/3")
EP4 = EpisodeRow(number=4.0, title="Ep 4", url="http://shinden.pl/ep/4")
EP5 = EpisodeRow(number=5.0, title="Ep 5", url="http://shinden.pl/ep/5")
EP6 = EpisodeRow(number=6.0, title="Ep 6", url="http://shinden.pl/ep/6")
EP7 = EpisodeRow(number=7.0, title="Ep 7", url="http://shinden.pl/ep/7")
# numeric ids, so the same entries also survive a round trip through the real episode-page parser
PLAYER = PlayerEntry(online_id="101", player="Sibnet", lang_audio="jp", lang_subs="pl")
PLAYER2 = PlayerEntry(online_id="102", player="CDA", lang_audio="jp", lang_subs="pl")
PLAYER3 = PlayerEntry(online_id="103", player="Mega", lang_audio="jp", lang_subs="pl")

STREAM = make_stream("https://cdn.example.com/v.m3u8", ext="m3u8")
STREAM_B = make_stream("https://cdn.example.com/b.mp4", ext="mp4")
EMBED = EmbedURL(url="https://www.filemoon.sx/e/abc", referer="https://shinden.pl/")

PLAYERS_CHANGED = ShindenXhrError("changed", stage="token_refresh", category="player_list_changed", reason="player_not_listed")


def xhr_error(**fields) -> ShindenXhrError:
    defaults = dict(stage="player_load", category="http_error", reason="http_status")
    return ShindenXhrError("xhr failed", **(defaults | fields))


@contextmanager
def noop_spinner(msg):
    yield


def resolved_for(player: PlayerEntry = PLAYER, stream: Stream = STREAM, embed: EmbedURL = EMBED) -> ResolvedStream:
    return ResolvedStream(stream=stream, source=player, embed=embed)


@dataclass(frozen=True)
class ResolveCall:
    chosen: PlayerEntry
    candidates: tuple[PlayerEntry, ...]
    policy: FallbackPolicy
    options: ExtractOptions | None
    force_fresh: bool


class FakeEpisodeSession:
    """Scripted EpisodeSession: ``resolve_script``/``probe_script`` hold one outcome, a list of them, or a callable."""

    def __init__(self, episode: EpisodeRow, players: Sequence[PlayerEntry] = (), *, resolve=None, probe=None) -> None:
        self.episode = episode
        self.sources = tuple(players)
        self.known_sources: dict[str, PlayerSource] = {}
        self.reload_used = False
        self.reloaded_sources: Sequence[PlayerEntry] | None = None
        self.resolve_script = resolve
        self.probe_script = probe
        self.select_calls: list[tuple[SourceFilter, Purpose]] = []
        self.resolve_calls: list[ResolveCall] = []
        self.probe_calls: list[list[PlayerEntry]] = []
        self.reload_calls = 0

    def select(self, flt: SourceFilter, *, purpose: Purpose) -> SourceSelection:
        self.select_calls.append((flt, purpose))
        return EpisodeSession.select(self, flt, purpose=purpose)

    def resolve(self, chosen, *, candidates=(), policy=FallbackPolicy.SINGLE, options=None, force_fresh=False):
        call = ResolveCall(chosen, tuple(candidates), policy, options, force_fresh)
        self.resolve_calls.append(call)
        outcome = _next_outcome(self, "resolve_script", call)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            outcome = STREAM
        return resolved_for(chosen, outcome) if isinstance(outcome, Stream) else outcome

    def probe_hosts(self, candidates) -> ShindenXhrError | None:
        self.probe_calls.append(list(candidates))
        return _next_outcome(self, "probe_script", list(candidates))

    def reload(self) -> None:
        if self.reload_used:
            raise ReloadLimitError("the player list was already reloaded once")
        self.reload_used = True
        self.reload_calls += 1
        self.known_sources = {}
        if self.reloaded_sources is not None:
            self.sources = tuple(self.reloaded_sources)


def _next_outcome(session: FakeEpisodeSession, name: str, call):
    script = getattr(session, name)
    if isinstance(script, list):
        if not script:
            raise AssertionError(f"{name} ran out of scripted outcomes")
        script = script.pop(0)
    return script(call) if callable(script) else script


def serve_players(engine: MagicMock, players: Sequence[PlayerEntry], *, configure: Callable | None = None) -> None:
    """Every ``open_episode`` returns a new FakeEpisodeSession, kept in ``engine.opened``."""
    engine.opened = []

    def open_episode(ep: EpisodeRow, **_kwargs) -> FakeEpisodeSession:
        session = FakeEpisodeSession(ep, players)
        if configure is not None:
            configure(session)
        engine.opened.append(session)
        return session

    engine.open_episode.side_effect = open_episode


class RecordingHistory(InMemoryHistory):
    def __init__(self) -> None:
        super().__init__()
        self.watch_calls: list[tuple[SeriesRef, float]] = []
        self.download_calls: list[tuple[SeriesRef, float]] = []

    def record_watch(self, series: SeriesRef, episode: float) -> None:
        self.watch_calls.append((series, episode))
        super().record_watch(series, episode)

    def record_download(self, series: SeriesRef, episode: float) -> None:
        self.download_calls.append((series, episode))
        super().record_download(series, episode)


def fake_engine(players: Sequence[PlayerEntry] = (), *, history=None) -> MagicMock:
    engine = MagicMock(spec=Engine)
    engine.history = history if history is not None else RecordingHistory()
    engine.events = RecordingSink()
    serve_players(engine, players)
    return engine


def make_state(*, options: CliOptions | None = None, engine=None, **fields) -> FlowState:
    """Fields past EPISODE_DISPATCH get the session dispatch would have opened, unless ``session`` is given."""
    state = FlowState(options=options or CliOptions(), engine=engine or fake_engine(), paths=paths.load_paths())
    for name, value in fields.items():
        setattr(state, name, value)
    if "session" not in fields and state.current_ep is not None:
        state.session = FakeEpisodeSession(state.current_ep, state.players)
    return state


def download_state(players=(PLAYER, PLAYER2), **fields) -> FlowState:
    defaults = dict(
        ref=SERIES_REF,
        targets=[EP1, EP2],
        ep_idx=0,
        players=list(players),
        chosen_player=players[0],
        episode_action="download",
    )
    defaults.update(fields)
    if "resolved" not in defaults:
        defaults["resolved"] = resolved_for(defaults["chosen_player"]) if defaults["chosen_player"] else None
    return make_state(**defaults)


def batch_state(targets, **fields) -> FlowState:
    return download_state(targets=list(targets), **fields)


class FakeDownloads:
    """Stands in for download.run: consults confirm_overwrite for 'existing' episodes like the real one does."""

    def __init__(self, existing=(), fail_urls=()) -> None:
        self.existing = set(existing)
        self.fail_urls = set(fail_urls)
        self.outcomes: list[tuple[str, float]] = []
        self.calls: list[dict] = []

    @staticmethod
    def path(dest_dir: Path, ep: EpisodeRow) -> Path:
        return dest_dir / f"Fate - ep{ep.number:g}.mp4"

    def __call__(self, variant, ep, ref, dest_dir, *, confirm_overwrite=None, events=None):
        self.calls.append({"variant": variant, "ep": ep, "ref": ref, "dest_dir": dest_dir, "events": events})
        replacing = False
        if confirm_overwrite is not None and ep.number in self.existing:
            if not confirm_overwrite(self.path(dest_dir, ep)):
                self.outcomes.append(("kept", ep.number))
                return False
            replacing = True
        if variant.url in self.fail_urls:
            self.outcomes.append(("failed", ep.number))
            raise DownloadFailedError("x")
        self.outcomes.append(("replaced" if replacing else "downloaded", ep.number))
        return True


def episode_html(players: Sequence[PlayerEntry], *, trap_id: int | None = None, token: str | None = None) -> str:
    """A minimal Shinden 2.5 episode page the real parser accepts; ``token`` adds the new-XHR auth island."""
    data = {
        "players": [
            {
                "online_id": int(p.online_id),
                "player": p.player,
                "audio": p.lang_audio,
                "sub": p.lang_subs,
                "res": int(p.max_res.removesuffix("p")) if p.max_res else None,
                "group": {"name": p.subs_author, "url": p.source},
            }
            for p in players
        ],
        "pulapka": trap_id,
        "xhr": "//shinden.pl/xhr",
    }
    rows = "".join(f'<li class="ep-pl-row" data-online-id="{p.online_id}"></li>' for p in players)
    island = f'<script type="application/json" id="episode-players-data">{json.dumps(data)}</script>'
    auth = f'<div class="ep-players__island" data-player-auth="{token}"></div>' if token else ""
    return f'<html><body>{auth}<ul class="ep-pl-list">{rows}</ul>{island}</body></html>'


def iframe(url: str) -> str:
    return f'<iframe src="{url}"></iframe>'


def in_turn(*replies):
    """A transport reply handing out ``replies`` one per request; the last one repeats."""
    pending = list(replies)

    def reply(_request: FakeRequest):
        return pending.pop(0) if len(pending) > 1 else pending[0]

    return reply


class FakeShinden:
    """Routes a FakeTransport like shinden.pl: episode pages and the player_load/player_show pair of each player."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport

    def episode(self, ep: EpisodeRow, page) -> None:
        self.transport.route(lambda request: request.url == ep.url, page)

    def player(self, player: PlayerEntry | str, *, embed: str = EMBED.url, load="0", show=None) -> None:
        online_id = player if isinstance(player, str) else player.online_id
        self.transport.route(lambda request: f"/xhr/{online_id}/player_load" in request.url, load)
        self.transport.route(
            lambda request: f"/xhr/{online_id}/player_show" in request.url, show if show is not None else iframe(embed)
        )

    def xhr_ids(self, stage: str) -> list[str]:
        return [url.split("/xhr/")[1].split("/")[0] for url in self.transport.urls if f"/{stage}?" in url]

    def page_gets(self, ep: EpisodeRow) -> int:
        return self.transport.urls.count(ep.url)


def http(status: int, body: str = "", headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(text=body, status_code=status, headers=headers or {})


def run_download(state: FlowState, fake: FakeDownloads, choice: str | None = None):
    """RUN_ACTION with download.run replaced by ``fake``; returns the next screen and the existing-file prompt mock."""
    with (
        patch("alt_ani_cli.flow.handlers.download.run", side_effect=fake),
        patch("alt_ani_cli.ui.menus.select_existing_download_action", return_value=choice) as mock_prompt,
        patch("alt_ani_cli.ui.progress.error"),
        patch("alt_ani_cli.ui.progress.warn"),
        patch("alt_ani_cli.ui.progress.info"),
    ):
        result = handle_run_action(state)
    return result, mock_prompt
