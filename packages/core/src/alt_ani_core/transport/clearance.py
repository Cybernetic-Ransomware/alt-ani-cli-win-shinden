"""Cloudflare clearance for shinden.pl: a disk cache plus an optional FlareSolverr round-trip."""

import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from alt_ani_core.config import SHINDEN_BASE, AccessConfig
from alt_ani_core.events import NULL_SINK, ClearanceRequested, ClearanceUnavailable, EventSink
from alt_ani_core.transport.api import SessionProfile

_EXPIRY_MARGIN = 300  # refresh 5 min before cookie expiry
FLARESOLVERR_TIMEOUT_SEC = 70
_CACHE_NAME = "cf_clearance.json"


@dataclass(frozen=True)
class Clearance:
    cf_clearance: str = field(repr=False)
    user_agent: str


def _cache_file(cache_dir: Path) -> Path:
    return cache_dir / _CACHE_NAME


def load_cached(cache_dir: Path) -> Clearance | None:
    try:
        data = json.loads(_cache_file(cache_dir).read_text(encoding="utf-8"))
        if time.time() < data.get("expires", 0) - _EXPIRY_MARGIN:
            return Clearance(data["cf_clearance"], data["user_agent"])
    except Exception:  # nosec B110
        pass
    return None


def save_cached(cache_dir: Path, clearance: Clearance, expires: float) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_file(cache_dir).write_text(
            json.dumps({"cf_clearance": clearance.cf_clearance, "user_agent": clearance.user_agent, "expires": expires}),
            encoding="utf-8",
        )
    except Exception:  # nosec B110
        pass


def fetch_via_flaresolverr(solver_url: str, cache_dir: Path) -> Clearance | None:
    """stdlib urllib on purpose: FlareSolverr is local infrastructure, not behind Cloudflare."""
    try:
        payload = json.dumps({"cmd": "request.get", "url": f"{SHINDEN_BASE}/", "maxTimeout": 60000}).encode()
        req = urllib.request.Request(f"{solver_url}/v1", data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=FLARESOLVERR_TIMEOUT_SEC) as resp:  # nosec B310
            data = json.loads(resp.read())
    except Exception:
        return None

    if data.get("status") != "ok":
        return None

    sol = data.get("solution", {})
    user_agent = sol.get("userAgent", "")
    for cookie in sol.get("cookies", []):
        if cookie.get("name") == "cf_clearance":
            expires = float(cookie.get("expires", -1))
            if expires < 0:
                expires = time.time() + 3600
            clearance = Clearance(cookie["value"], user_agent)
            save_cached(cache_dir, clearance, expires)
            return clearance
    return None


def get_clearance(access: AccessConfig, cache_dir: Path, events: EventSink = NULL_SINK) -> Clearance | None:
    """Cached clearance if still valid, else a FlareSolverr round-trip; a direct-only policy uses neither."""
    if not access.use_flaresolverr:
        return None
    cached = load_cached(cache_dir)
    if cached is not None:
        return cached
    solver_url = access.solver_url
    if not solver_url:
        return None
    events.emit(ClearanceRequested())
    clearance = fetch_via_flaresolverr(solver_url, cache_dir)
    if clearance is None:
        events.emit(ClearanceUnavailable())
    return clearance


def shinden_profile(clearance: Clearance | None) -> SessionProfile:
    headers = {"Referer": f"{SHINDEN_BASE}/", "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"}
    # _rnd is a Unix timestamp shinden uses as a lightweight session seed; it must be present from the first request.
    cookies = {"_rnd": str(int(time.time()))}
    if clearance is not None:
        cookies["cf_clearance"] = clearance.cf_clearance
        if clearance.user_agent:
            headers["User-Agent"] = clearance.user_agent
    return SessionProfile(name="shinden", headers=headers, cookies=cookies)
