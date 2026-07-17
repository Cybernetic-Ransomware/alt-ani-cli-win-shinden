class ShindenError(Exception):
    """Base exception for all alt-ani-cli errors."""


class AntiBotError(ShindenError):
    """shinden API returned 403 or empty iframe — guest token likely expired."""


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


class FilterMismatchError(ShindenError):
    """--lang/--subs/--player-name matched no players and --allow-fallback was not set."""
