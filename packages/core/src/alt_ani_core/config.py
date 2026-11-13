"""Protocol constants and explicit engine configuration; nothing here reads the environment or the filesystem."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Hardcoded guest token: base64(_guest_:0,5,21000000,255,4174293644)
# URL-encoded so it's safe to drop straight into query strings.
GUEST_AUTH = "X2d1ZXN0XzowLDUsMjEwMDAwMDAsMjU1LDQxNzQyOTM2NDQ%3D"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SHINDEN_BASE = "https://shinden.pl"
SHINDEN_API_BASE = "https://api4.shinden.pl"
# The episode page names its XHR endpoint, but only this one is ever called; the page value is checked against it.
SHINDEN_XHR_BASE = f"{SHINDEN_BASE}/xhr"

# Minimum wait between player_load and player_show; the server's own value wins when it is longer.
DEFAULT_ANTIBOT_DELAY_SEC = 5.0
# A player_load wait above this is treated as a malformed response, never clamped.
MAX_XHR_WAIT_SEC = 30

HTTP_TIMEOUT_SEC = 30.0
YTDLP_SOCKET_TIMEOUT_SEC = 30.0

XhrBackendName = Literal["new", "api4"]


@dataclass(frozen=True)
class AccessConfig:
    """How the engine may obtain Cloudflare clearance; the platform decides this from the network it is on."""

    flaresolverr_url: str | None = None
    use_flaresolverr: bool = True

    @property
    def solver_url(self) -> str | None:
        return self.flaresolverr_url if self.use_flaresolverr and self.flaresolverr_url else None


@dataclass(frozen=True)
class ExtractOptions:
    """yt-dlp cookie sources; cookies_browser reads local browser profiles and is desktop-only."""

    cookies_file: str | None = None
    cookies_browser: str | None = None


@dataclass(frozen=True)
class EngineConfig:
    cache_dir: Path
    xhr_backend: XhrBackendName = "new"
    antibot_delay_sec: float = DEFAULT_ANTIBOT_DELAY_SEC
    access: AccessConfig = field(default_factory=AccessConfig)
