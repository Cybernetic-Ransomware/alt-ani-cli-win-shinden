class ShindenError(Exception):
    """Base exception for all alt-ani-cli errors."""


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
    """Could not extract a playable video URL from the embed page."""


class UnsupportedHostError(NoStreamError):
    """Host is known to be unextractable — the registry marks it unsupported."""


class JavaScriptRequiredError(UnsupportedHostError):
    """Host serves a pure JS SPA — needs a real browser, no static video URL."""


class PlayerNotFoundError(ShindenError):
    """mpv / vlc executable not found on PATH."""


class ParseError(ShindenError):
    """HTML parsing produced unexpected structure."""


class IncompleteEpisodeListError(ShindenError):
    """Episode list pagination stopped early, so episode selection cannot trust it as the full catalogue."""


class FilterMismatchError(ShindenError):
    """--lang/--subs/--player-name matched no players and --allow-fallback was not set."""


class DownloadFailedError(ShindenError):
    """yt-dlp could not download the stream; carries no library exception or URL."""


class DownloadTargetError(ShindenError):
    """A redownload could not be put in place on disk; the source is not at fault and the old file is intact."""
