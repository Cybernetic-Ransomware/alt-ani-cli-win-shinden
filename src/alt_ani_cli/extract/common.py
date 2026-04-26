from dataclasses import dataclass, field


@dataclass
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    qualities: dict[str, str] = field(default_factory=dict)  # "1080p" → direct url
    ext: str = "mp4"
