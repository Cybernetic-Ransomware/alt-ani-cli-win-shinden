"""Helpers shared by the CLI tests: a fake shinden.pl site on FakeTransport and small builders."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from unittest.mock import MagicMock

from alt_ani_core.config import SHINDEN_API_BASE, SHINDEN_XHR_BASE
from alt_ani_core.models import EpisodeRow, PlayerEntry, SeriesRef
from alt_ani_core.services.engine import Engine
from alt_ani_core.testing import FakeRequest, FakeTransport, Reply, make_stream

from alt_ani_cli.args import CliOptions

SERIES_URL = "https://shinden.pl/series/1-fate"
REF = SeriesRef(id="1", slug="fate", title="Fate", url=SERIES_URL)
TEST_TOKEN = "VGVzdFRva2VuRm9yVW5pdFRlc3RzT25seQ+/=="
STREAM = make_stream("https://cdn.example/video.mp4")


def episode(number: int) -> EpisodeRow:
    return EpisodeRow(number=float(number), title=f"Ep {number}", url=f"https://shinden.pl/episode/1-fate/view/{100 + number}")


def player(online_id: str, name: str, audio: str = "jp", subs: str = "pl", res: int | None = None) -> PlayerEntry:
    return PlayerEntry(online_id=online_id, player=name, lang_audio=audio, lang_subs=subs, max_res=f"{res}p" if res else None)


def episodes_html(
    episodes: Sequence[EpisodeRow], *, title: str = REF.title, count: int | None = None, last_page: int = 1
) -> str:
    """The 2.5 episode-list markup reduced to what list_episodes reads; rows are newest-first like the real page."""
    rows = "".join(
        f'<li class="ep-row"><a class="ep-row__link" href="{ep.url.removeprefix("https://shinden.pl")}">'
        f'<span class="ep-row__no">{ep.number:g}</span><span class="ep-row__title">{ep.title}</span></a></li>'
        for ep in reversed(episodes)
    )
    pages = "".join(f'<a href="/series/1-fate/episodes?strona={n}#odcinki">{n}</a>' for n in range(1, last_page + 1))
    total = len(episodes) if count is None else count
    return (
        f'<html><head><title>{title} - Shinden</title></head><body><h1 class="title">{title}</h1>'
        f'<section id="odcinki"><h2>Odcinki <span class="ep-list__count">{total}</span></h2>'
        f'<nav class="ep-pages">{pages}</nav><ol class="ep-rows">{rows}</ol></section></body></html>'
    )


def players_html(players: Sequence[PlayerEntry], *, token: str | None = TEST_TOKEN) -> str:
    """An episode page with the player JSON island; without a token the episode falls back to api4."""
    data = {
        "players": [
            {
                "online_id": int(p.online_id),
                "player": p.player,
                "audio": p.lang_audio,
                "sub": p.lang_subs,
                "res": int(p.max_res.removesuffix("p")) if p.max_res else None,
            }
            for p in players
        ],
        "pulapka": None,
        "xhr": "//shinden.pl/xhr",
    }
    auth = f' data-player-auth="{token}"' if token is not None else ""
    rows = "".join(f'<li class="ep-pl-row" data-online-id="{p.online_id}">{p.player}</li>' for p in players)
    return (
        f"<html><head><title>Episode</title></head><body>"
        f'<div class="ep-players__island"{auth}><ul class="ep-pl-list">{rows}</ul></div>'
        f'<script type="application/json" id="episode-players-data">{json.dumps(data)}</script></body></html>'
    )


def is_xhr(url: str) -> bool:
    return url.startswith((SHINDEN_XHR_BASE + "/", SHINDEN_API_BASE + "/xhr/"))


def xhr_online_id(url: str) -> str:
    return url.split("/xhr/", 1)[1].split("/", 1)[0]


@dataclass
class Site:
    """Routes a FakeTransport like shinden.pl: one series, its episode pages and the player XHR of both backends."""

    transport: FakeTransport
    embeds: dict[str, str] = field(default_factory=dict)

    def series(self, episodes: Sequence[EpisodeRow], **listing: object) -> None:
        self.transport.route(f"{SERIES_URL}/episodes", episodes_html(episodes, **listing))  # ty: ignore[invalid-argument-type]

    def episode(self, ep: EpisodeRow, players: Sequence[PlayerEntry], *, token: str | None = TEST_TOKEN) -> None:
        self.transport.route(ep.url, players_html(players, token=token))
        for p in players:
            self.embeds.setdefault(p.online_id, f"https://{p.player.lower()}.example/e/{p.online_id}")

    def embed(self, online_id: str, url: str) -> None:
        self.embeds[online_id] = url

    def xhr(self, reply: Reply | None = None) -> None:
        """Default: player_load answers a zero wait and player_show an iframe with the player's embed URL."""
        self.transport.route(lambda request: is_xhr(request.url), reply if reply is not None else self._xhr_ok)

    def _xhr_ok(self, request: FakeRequest) -> str:
        if "/player_load" in request.url:
            return "0"
        return f'<iframe src="{self.embeds[xhr_online_id(request.url)]}"></iframe>'

    @property
    def xhr_urls(self) -> list[str]:
        return [url for url in self.transport.urls if is_xhr(url)]

    @property
    def shown_ids(self) -> list[str]:
        return [xhr_online_id(url) for url in self.xhr_urls if "/player_show" in url]

    @property
    def episode_urls(self) -> list[str]:
        return [url for url in self.transport.urls if "/episode/" in url]


class ConsoleLog:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def of(self, level: str) -> list[str]:
        return [text for kind, text in self.messages if kind == level]

    @property
    def text(self) -> str:
        return "\n".join(text for _, text in self.messages)


def options(**overrides: object) -> CliOptions:
    return CliOptions(**({"url": SERIES_URL} | overrides))  # ty: ignore[invalid-argument-type]


def extracted_urls(extract: MagicMock) -> list[str]:
    return [call.args[0] for call in extract.call_args_list]


@dataclass
class MainResult:
    code: int | None
    engines: list[Engine]

    @property
    def engine(self) -> Engine:
        (engine,) = self.engines
        return engine
