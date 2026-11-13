"""Desktop locations and environment settings; the only CLI module that reads them."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_videos_dir

from alt_ani_core.config import DEFAULT_ANTIBOT_DELAY_SEC, AccessConfig, EngineConfig
from alt_ani_core.shinden import xhr as shinden_xhr

APP = "alt-ani-cli"

ENV_XHR_BACKEND = "ALT_ANI_CLI_XHR_BACKEND"
ENV_ANTIBOT_DELAY = "ALT_ANI_CLI_ANTIBOT_DELAY"
ENV_FLARESOLVERR_URL = "ALT_ANI_CLI_FLARESOLVERR_URL"


@dataclass(frozen=True)
class CliPaths:
    state_dir: Path
    cache_dir: Path
    downloads_dir: Path

    @property
    def history_db(self) -> Path:
        return self.state_dir / "history.db"

    @property
    def legacy_history(self) -> Path:
        return self.state_dir / "history.json"

    @property
    def diagnostics_dir(self) -> Path:
        return self.cache_dir / "diagnostics"

    @property
    def mpv_log(self) -> Path:
        return self.cache_dir / "mpv-debug.log"


def load_paths() -> CliPaths:
    return CliPaths(
        state_dir=Path(user_data_dir(APP, APP)),
        cache_dir=Path(user_cache_dir(APP, APP)),
        downloads_dir=Path(user_videos_dir()) / APP,
    )


def flaresolverr_url(environ: Mapping[str, str] | None = None) -> str:
    return (os.environ if environ is None else environ).get(ENV_FLARESOLVERR_URL, "")


def load_engine_config(paths: CliPaths, environ: Mapping[str, str] | None = None) -> EngineConfig:
    """Raises ValueError for a bad setting, before any request is made."""
    env = os.environ if environ is None else environ
    backend = env.get(ENV_XHR_BACKEND, "new")
    shinden_xhr.parse_backend(backend)
    raw_delay = env.get(ENV_ANTIBOT_DELAY)
    try:
        delay = float(raw_delay) if raw_delay is not None else DEFAULT_ANTIBOT_DELAY_SEC
    except ValueError:
        raise ValueError(f"{ENV_ANTIBOT_DELAY} must be a number of seconds, got {raw_delay!r}.") from None
    return EngineConfig(
        cache_dir=paths.cache_dir,
        xhr_backend=backend.strip().lower(),  # ty: ignore[invalid-argument-type]
        antibot_delay_sec=delay,
        access=AccessConfig(flaresolverr_url=flaresolverr_url(env) or None),
    )
