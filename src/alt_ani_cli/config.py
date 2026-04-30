import os
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir, user_videos_dir

APP = "alt-ani-cli"

STATE_DIR = Path(user_data_dir(APP, APP))
CACHE_DIR = Path(user_cache_dir(APP, APP))
DOWNLOADS = Path(user_videos_dir()) / APP
HISTORY_FILE = STATE_DIR / "history.json"
COOKIES_FILE = STATE_DIR / "cookies.json"

# Hardcoded guest token: base64(_guest_:0,5,21000000,255,4174293644)
# URL-encoded so it's safe to drop straight into query strings.
GUEST_AUTH = "X2d1ZXN0XzowLDUsMjEwMDAwMDAsMjU1LDQxNzQyOTM2NDQ%3D"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

SHINDEN_BASE = "https://shinden.pl"
SHINDEN_API_BASE = "https://api4.shinden.pl"

# Increase via env var if player_show keeps returning empty responses.
ANTIBOT_DELAY_SEC = float(os.getenv("ALT_ANI_CLI_ANTIBOT_DELAY", "5.0"))
