"""Flow state: Screen enum, FlowState dataclass, BACK sentinel."""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal

from alt_ani_core.models import EpisodeRow, PlayerEntry, ResolvedStream, SeriesHit, SeriesRef
from alt_ani_core.rules.pin import PlayerFingerprint
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession

from alt_ani_cli.args import CliOptions
from alt_ani_cli.paths import CliPaths


class Screen(Enum):
    START_MODE = auto()
    SEARCH_QUERY = auto()
    URL_INPUT = auto()
    RESUME_PICK = auto()
    DOWNLOAD_RESUME_PICK = auto()
    SERIES_PICK = auto()
    FETCH_EPISODES = auto()  # virtual — I/O without UI
    EPISODES_PICK = auto()
    EPISODE_DISPATCH = auto()  # virtual — picks the next episode from targets
    PLAYER_PICK = auto()
    RESOLVE_STREAM = auto()  # virtual — network without UI
    QUALITY_PICK = auto()
    ACTION_PICK = auto()
    RUN_ACTION = auto()  # virtual — playback/download


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
    """Navigation, batch and UI choices only; everything about an opened episode lives in its EpisodeSession."""

    options: CliOptions
    engine: Engine
    paths: CliPaths

    # search / series
    query: str | None = None
    hits: list[SeriesHit] = field(default_factory=list)
    ref: SeriesRef | None = None
    last_ep: float = 0.0
    resume_mode: Literal["watch", "download"] | None = None
    downloaded_eps: set[float] = field(default_factory=set)

    # episodes
    episodes: list[EpisodeRow] = field(default_factory=list)
    # False when pagination stopped early — the oldest episodes are missing, so nothing may be auto-selected
    episodes_complete: bool = True
    targets: list[EpisodeRow] = field(default_factory=list)
    ep_idx: int = 0
    completed_eps: set[float] = field(default_factory=set)
    # replace already-downloaded files without asking — scoped to the current targets batch only
    overwrite_existing_batch: bool = False
    episode_arg_consumed: bool = False
    # download source pin — scoped to the current targets batch, never persisted
    pinned_player: PlayerFingerprint | None = None

    # the opened episode; replaced on every dispatch except the one internal reload after PLAYERS_CHANGED
    session: EpisodeSession | None = None
    reload_pending: bool = False

    # player / stream
    players: list[PlayerEntry] = field(default_factory=list)
    chosen_player: PlayerEntry | None = None
    # True only when chosen_player came from PLAYER_PICK (not pin / --select-nth / single-player auto-pick)
    player_picked_manually: bool = False
    failed_ids: set[str] = field(default_factory=set)
    # sources whose stream resolved but then failed to download: picking one again forces a fresh resolve
    expired_ids: set[str] = field(default_factory=set)
    resolved: ResolvedStream | None = None

    # cached user choices
    quality: str | None = None
    episode_action: str | None = None

    @property
    def current_ep(self) -> EpisodeRow | None:
        return self.targets[self.ep_idx] if self.ep_idx < len(self.targets) else None
