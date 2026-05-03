"""Flow state: Screen enum, FlowState dataclass, BACK sentinel."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class Screen(Enum):
    START_MODE = auto()
    SEARCH_QUERY = auto()
    URL_INPUT = auto()
    RESUME_PICK = auto()
    SERIES_PICK = auto()
    FETCH_EPISODES = auto()  # wirtualny — I/O bez UI
    EPISODES_PICK = auto()
    EPISODE_DISPATCH = auto()  # wirtualny — wybiera następny ep z targets
    PLAYER_PICK = auto()
    RESOLVE_STREAM = auto()  # wirtualny — sieć bez UI
    QUALITY_PICK = auto()
    ACTION_PICK = auto()
    RUN_ACTION = auto()  # wirtualny — odtwarzanie/pobranie


_VIRTUAL_SCREENS: frozenset[Screen] = frozenset(
    {
        # No UI — I/O or computation only:
        Screen.FETCH_EPISODES,
        Screen.EPISODE_DISPATCH,
        Screen.RESOLVE_STREAM,
        Screen.RUN_ACTION,
        # Per-episode navigation — these handle ESC internally by returning a
        # concrete Screen rather than BACK, so they must not be pushed onto the
        # global history stack (which only tracks series-level navigation):
        Screen.PLAYER_PICK,
        Screen.QUALITY_PICK,
        Screen.ACTION_PICK,
    }
)


class _BackSentinel:
    pass


BACK = _BackSentinel()

ScreenResult = Screen | _BackSentinel | None


@dataclass
class FlowState:
    args: Any
    client: Any

    # search / series
    query: str | None = None
    hits: list = field(default_factory=list)
    ref: Any = None
    last_ep: float = 0.0

    # episodes
    episodes: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    ep_idx: int = 0
    completed_eps: set[float] = field(default_factory=set)

    # player / stream
    players: list = field(default_factory=list)
    chosen_player: Any = None
    failed_ids: set[str] = field(default_factory=set)
    stream: Any = None
    embed: Any = None

    # cached user choices
    quality: str | None = None
    episode_action: str | None = None

    @property
    def current_ep(self):
        return self.targets[self.ep_idx] if self.ep_idx < len(self.targets) else None
