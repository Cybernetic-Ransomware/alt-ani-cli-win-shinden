class ShindenError(Exception):
    """Base exception for all alt-ani-cli errors."""


class AntiBotError(ShindenError):
    """shinden API returned 403 or empty iframe — guest token likely expired."""


class NoStreamError(ShindenError):
    """Could not extract a playable video URL from the embed page."""


class PlayerNotFoundError(ShindenError):
    """mpv / vlc executable not found on PATH."""


class ParseError(ShindenError):
    """HTML parsing produced unexpected structure."""
