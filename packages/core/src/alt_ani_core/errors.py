from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alt_ani_core.models import ExtractionFailure


class ShindenError(Exception):
    """Base exception for all engine errors; ``code`` and ``params`` let an application render its own text."""

    code: str | None = None
    params: Mapping[str, object] = {}

    def coded(self, code: str, **params: object):
        self.code = code
        self.params = params
        return self


class AntiBotError(ShindenError):
    """shinden API returned 403 or empty iframe — guest token likely expired."""


class ShindenXhrError(ShindenError):
    """player_load/player_show failed; carries only classified primitives, never the URL, body or server message."""

    layer = "shinden_api"

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        category: str,
        reason: str,
        http_status: int | None = None,
        api_code: str | None = None,
        retry_after: int | None = None,
        backend: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.reason = reason
        self.http_status = http_status
        self.api_code = api_code
        self.retry_after = retry_after
        self.backend = backend


class NoStreamError(ShindenError):
    """Could not extract a playable video URL from the embed page; ``failure`` is set by the extractor dispatch."""

    failure: ExtractionFailure | None = None


class UnsupportedHostError(NoStreamError):
    """Host is known to be unextractable — the registry marks it unsupported."""


class JavaScriptRequiredError(UnsupportedHostError):
    """Host serves a pure JS SPA — needs a real browser, no static video URL."""


class PlayerNotFoundError(ShindenError):
    """mpv / vlc executable not found on PATH."""


class ParseError(ShindenError):
    """HTML parsing produced unexpected structure."""


class AccessBlockedError(ParseError):
    """Shinden served a protection page instead of content; a ParseError so existing handling of unreadable pages applies."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class IncompleteEpisodeListError(ShindenError):
    """Episode list pagination stopped early, so episode selection cannot trust it as the full catalogue."""


class FilterMismatchError(ShindenError):
    """--lang/--subs/--player-name matched no players and --allow-fallback was not set."""


class DownloadFailedError(ShindenError):
    """yt-dlp could not download the stream; carries no library exception or URL."""


class DownloadTargetError(ShindenError):
    """A redownload could not be put in place on disk; the source is not at fault and the old file is intact."""


class SourcesExhaustedError(ShindenError):
    """Every candidate source of the episode failed; ``attempts`` lists what was tried."""

    def __init__(self, message: str, *, attempts: tuple = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


class ReloadLimitError(ShindenError):
    """The episode's player list already got its one internal reload."""


class FreshResolveLimitError(ShindenError):
    """A source was force-resolved as often as one episode session allows."""


class HistoryVersionError(ShindenError):
    """The history database was written by a newer schema than this code knows."""


class HistoryBackupError(ShindenError):
    """The copy taken before a schema upgrade could not be written or verified; the database was left as it was."""


class StateConflictError(ShindenError):
    """The caller acted on a revision of local state that has changed since it was read."""


class InvalidTransitionError(ShindenError):
    """The requested change is not allowed from the record's current state."""


class UnknownRecordError(ShindenError):
    """No episode, job or offline item with the given identity is stored."""


class EngineClosedError(ShindenError):
    """The engine was closed and accepts no further operations."""


class EngineInterrupt(BaseException):
    """Not an Exception on purpose: extractor fallbacks and yt-dlp catch Exception and must not swallow these."""


class OperationCancelled(EngineInterrupt):
    """The caller cancelled the operation; nothing was recorded and no fallback ran."""


class StaleSessionError(EngineInterrupt):
    """The engine's access changed or it was closed while this operation ran; its result must be discarded."""
