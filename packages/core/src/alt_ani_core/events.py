"""Typed notices and progress the engine reports; applications render them, the engine never prints."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EngineEvent:
    pass


@dataclass(frozen=True)
class ClearanceRequested(EngineEvent):
    """A Cloudflare clearance is being solved through FlareSolverr (takes tens of seconds)."""


@dataclass(frozen=True)
class ClearanceUnavailable(EngineEvent):
    """FlareSolverr was configured but gave no clearance; the engine goes on with a direct connection."""


@dataclass(frozen=True)
class PhaseStarted(EngineEvent):
    phase: str


@dataclass(frozen=True)
class PhaseFinished(EngineEvent):
    phase: str


@dataclass(frozen=True)
class XhrFallback(EngineEvent):
    episode: float
    reason: str


@dataclass(frozen=True)
class EmbedResolved(EngineEvent):
    redacted_url: str


@dataclass(frozen=True)
class SourceAttemptFailed(EngineEvent):
    player: str
    episode: float
    redacted_text: str


@dataclass(frozen=True)
class HostDeferred(EngineEvent):
    host: str


@dataclass(frozen=True)
class HostLastResort(EngineEvent):
    host: str


@dataclass(frozen=True)
class HostDropped(EngineEvent):
    host: str


@dataclass(frozen=True)
class ExtractorFallback(EngineEvent):
    kind: str
    host: str
    redacted_text: str


@dataclass(frozen=True)
class DownloadStarted(EngineEvent):
    title: str
    episode_label: str
    directory: str


@dataclass(frozen=True)
class DownloadLog(EngineEvent):
    redacted_line: str


@dataclass(frozen=True)
class LegacyPartMoved(EngineEvent):
    path: str


@dataclass(frozen=True)
class DownloadSaved(EngineEvent):
    path: str


class EventSink(Protocol):
    def emit(self, event: EngineEvent) -> None: ...


class _NullSink:
    def emit(self, event: EngineEvent) -> None:
        pass


NULL_SINK: EventSink = _NullSink()
