from dataclasses import dataclass, field

from alt_ani_cli.failure import CATEGORY_NO_STREAM_URL as CATEGORY_NO_STREAM_URL
from alt_ani_cli.failure import CATEGORY_PARSER_DRIFT as CATEGORY_PARSER_DRIFT


@dataclass
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    qualities: dict[str, str] = field(default_factory=dict)  # "1080p" → direct url
    ext: str = "mp4"


class ExtractError(ValueError):
    """Stays a ValueError so existing ``except ValueError`` call sites keep working."""

    def __init__(self, message: str, category: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status
