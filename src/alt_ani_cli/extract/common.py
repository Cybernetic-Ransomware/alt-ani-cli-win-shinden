from dataclasses import dataclass, field


@dataclass
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    qualities: dict[str, str] = field(default_factory=dict)  # "1080p" → direct url
    ext: str = "mp4"


CATEGORY_PARSER_DRIFT = "parser_drift"  # our own regex/URL-shape assumption no longer matches what we received
CATEGORY_NO_STREAM_URL = "no_stream_url"  # request succeeded and parsed, but no playable URL was in it


class ExtractError(ValueError):
    """Stays a ValueError so existing ``except ValueError`` call sites keep working."""

    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category
