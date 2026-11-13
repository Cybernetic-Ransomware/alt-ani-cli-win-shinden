"""Persisted local state: watch progress, download jobs, offline items and preferences; nothing here holds a stream URL."""

import re
from dataclasses import dataclass, field
from enum import StrEnum

ANY = "any"
NO_SUBTITLES = "none"

_QUALITY_RE = re.compile(r"max:([1-9]\d{0,4})")


@dataclass(frozen=True)
class EpisodeKey:
    series_id: str
    number: float

    def __post_init__(self) -> None:
        if not self.series_id:
            raise ValueError("an episode key needs a series id")
        object.__setattr__(self, "number", float(self.number))


class OpenedVia(StrEnum):
    PLAYER = "player"
    BROWSER = "browser"


class WatchedSource(StrEnum):
    PLAYBACK_ENDED = "playback_ended"
    MANUAL = "manual"


@dataclass(frozen=True)
class WatchState:
    """Opening, position and the watched flag are independent; ``position_ms is None`` means unknown, not zero."""

    key: EpisodeKey
    last_opened_at: str | None = None
    opened_via: OpenedVia | None = None
    position_ms: int | None = None
    duration_ms: int | None = None
    position_at: str | None = None
    watched: bool = False
    watched_source: WatchedSource | None = None
    watched_at: str | None = None
    playback_session_id: str | None = None
    playback_seq: int | None = None
    revision: int = 0


@dataclass(frozen=True)
class PlaybackSession:
    """Only the newest session of an episode may record its position; ``seq`` of every write must grow."""

    key: EpisodeKey
    session_id: str


def normalize_language(value: str) -> str:
    return " ".join(value.split()).casefold()


def _criterion(value: str, *, allow_none: bool) -> str:
    norm = normalize_language(value)
    if not norm:
        raise ValueError("a language criterion cannot be empty")
    if norm == NO_SUBTITLES and not allow_none:
        raise ValueError("only subtitles can be required to be absent")
    return norm


@dataclass(frozen=True)
class QualityCriterion:
    """``max_height`` None is the best available; a height is an upper limit that unknown heights never satisfy."""

    max_height: int | None = None

    def __post_init__(self) -> None:
        if self.max_height is not None and (isinstance(self.max_height, bool) or self.max_height <= 0):
            raise ValueError("a quality limit must be a positive height")

    def encode(self) -> str:
        return "best" if self.max_height is None else f"max:{self.max_height}"

    @classmethod
    def parse(cls, text: str) -> QualityCriterion:
        if text == "best":
            return cls()
        if m := _QUALITY_RE.fullmatch(text):
            return cls(int(m.group(1)))
        raise ValueError(f"unknown quality criterion {text!r}")


@dataclass(frozen=True)
class DownloadCriteria:
    """``audio``: ``any`` or a language; ``subs``: ``any``, ``none`` (declared absent) or a language."""

    audio: str = ANY
    subs: str = ANY
    quality: QualityCriterion = field(default_factory=QualityCriterion)

    def __post_init__(self) -> None:
        object.__setattr__(self, "audio", _criterion(self.audio, allow_none=False))
        object.__setattr__(self, "subs", _criterion(self.subs, allow_none=True))


class AlternativesPolicy(StrEnum):
    OFF = "off"
    COMPATIBLE = "compatible"


class SelectionMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True)
class SourceIdentity:
    """Which player of an episode was chosen; hosts and names only, never an embed or stream URL."""

    online_id: str
    player: str
    lang_audio: str
    lang_subs: str
    max_res: str
    subs_author: str
    source: str


@dataclass(frozen=True)
class VariantIdentity:
    label: str
    height: int | None
    protocol: str
    container: str | None = None


@dataclass(frozen=True)
class ResourceValidator:
    """Non-secret evidence that a partial file belongs to the resource seen now: lengths, counts and digests."""

    kind: str
    fields: tuple[tuple[str, str | int], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(sorted(self.fields)))


@dataclass(frozen=True)
class StorageLocator:
    """Opaque to the engine: a path for ``fs``, a content URI or another key for a platform adapter."""

    backend: str
    key: str

    def __post_init__(self) -> None:
        if not self.backend or not self.key:
            raise ValueError("a storage locator needs a backend and a key")


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # a stop was requested; the attempt still owns its slot until the executor confirms its I/O ended
    STOPPING = "stopping"
    # the attempt was allowed to publish its file; only it can complete the job
    FINALIZING = "finalizing"
    PAUSED = "paused"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset({JobState.RUNNING, JobState.STOPPING, JobState.FINALIZING})
TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.CANCELLED})


class WaitReason(StrEnum):
    NETWORK = "network"
    INTERACTION = "interaction"
    STORAGE = "storage"
    SOURCE_DECISION = "source_decision"


class StopTarget(StrEnum):
    PAUSED = "paused"
    CANCELLED = "cancelled"
    WAITING = "waiting"
    QUEUED = "queued"


class AttemptStatus(StrEnum):
    CONTINUE = "continue"
    # the write was kept, but the executor must end its I/O and call ack_stopped
    STOP = "stop"
    # the attempt no longer owns the job: nothing was written and it must not touch the job's files
    LOST = "lost"


@dataclass(frozen=True)
class JobError:
    code: str
    params: tuple[tuple[str, str | int | float | bool | None], ...] = ()


@dataclass(frozen=True)
class PartialTransfer:
    locator: StorageLocator
    source: SourceIdentity
    variant: VariantIdentity
    validator: ResourceValidator | None = None


@dataclass(frozen=True)
class DownloadJob:
    id: str
    key: EpisodeKey
    seq: int
    queue_pos: int
    state: JobState
    criteria: DownloadCriteria
    alternatives: AlternativesPolicy
    selection_mode: SelectionMode
    created_at: str
    updated_at: str
    wait_reason: WaitReason | None = None
    stop_target: StopTarget | None = None
    stop_wait_reason: WaitReason | None = None
    source: SourceIdentity | None = None
    variant: VariantIdentity | None = None
    cellular_allowed: bool = False
    attempt_id: str | None = None
    attempts: int = 0
    bytes_done: int | None = None
    bytes_total: int | None = None
    partial: PartialTransfer | None = None
    target: StorageLocator | None = None
    last_error: JobError | None = None
    offline_item_id: str | None = None
    completed_at: str | None = None
    retry_count: int = 0
    not_before: str | None = None
    revision: int = 0


@dataclass(frozen=True)
class Claim:
    job: DownloadJob
    attempt_id: str


@dataclass(frozen=True)
class Recovery:
    """``reconcile`` holds new attempts for jobs found finalizing: the old attempts are void, the file may exist."""

    requeued: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    reconcile: tuple[Claim, ...] = ()


class SkipReason(StrEnum):
    ALREADY_QUEUED = "already_queued"
    ALREADY_DOWNLOADED = "already_downloaded"
    # a job for the episode was added after the preview the caller confirmed (possibly cancelled since)
    CHANGED_SINCE_PREVIEW = "changed_since_preview"


class JobAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"
    ALLOW_CELLULAR = "allow_cellular"
    CHOOSE_SOURCE = "choose_source"
    RETRY_INTERACTION = "retry_interaction"


@dataclass(frozen=True)
class EnqueuePlan:
    """``basis_seq`` is the newest job sequence the preview saw; ``enqueue`` uses it to detect later changes."""

    to_add: tuple[EpisodeKey, ...]
    skipped: tuple[tuple[EpisodeKey, SkipReason], ...]
    basis_seq: int = 0


@dataclass(frozen=True)
class JobSignal:
    """The fields a change watcher compares; ``revision`` grows with every write of the job, progress included."""

    job_id: str
    revision: int
    state: JobState
    wait_reason: WaitReason | None
    bytes_done: int | None
    bytes_total: int | None


@dataclass(frozen=True)
class EnqueueResult:
    added: tuple[DownloadJob, ...]
    skipped: tuple[tuple[EpisodeKey, SkipReason], ...]


class ItemStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    DELETING = "deleting"


@dataclass(frozen=True)
class OfflineItemSpec:
    """What the executor actually saved: the real language, subtitles and height, not the requested criteria."""

    locator: StorageLocator
    size_bytes: int | None = None
    audio: str | None = None
    subs: str | None = None
    height: int | None = None
    variant_label: str | None = None
    protocol: str | None = None
    container: str | None = None
    source_player: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.sha256 is not None and (len(self.sha256) != 64 or self.sha256.strip("0123456789abcdef")):
            raise ValueError("sha256 must be 64 lowercase hex digits")


@dataclass(frozen=True)
class OfflineItem:
    id: str
    key: EpisodeKey
    status: ItemStatus
    spec: OfflineItemSpec
    created_at: str
    verified_at: str | None = None
    revision: int = 0


class RemovalReason(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RESTARTED = "restarted"
    SOURCE_CHANGED = "source_changed"


@dataclass(frozen=True)
class PendingRemoval:
    """A file or partial directory the executor created and abandoned; only these are ever removed automatically."""

    id: str
    locator: StorageLocator
    reason: RemovalReason
    created_at: str
    job_id: str | None = None


class DownloadNetwork(StrEnum):
    WIFI_ONLY = "wifi_only"
    ANY = "any"


class NetworkKind(StrEnum):
    WIFI = "wifi"
    CELLULAR = "cellular"
    OFFLINE = "offline"


@dataclass(frozen=True)
class Preferences:
    audio: str = ANY
    subs: str = ANY
    quality: QualityCriterion = field(default_factory=QualityCriterion)
    download_network: DownloadNetwork = DownloadNetwork.WIFI_ONLY
    auto_alternatives: bool = False
    max_active_downloads: int = 1
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "audio", _criterion(self.audio, allow_none=False))
        object.__setattr__(self, "subs", _criterion(self.subs, allow_none=True))
        if isinstance(self.max_active_downloads, bool) or self.max_active_downloads < 1:
            raise ValueError("at least one download must be allowed to run")
