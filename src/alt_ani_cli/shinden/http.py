import json
import sys
import time
import urllib.request

from curl_cffi import requests as cffi_requests

from alt_ani_cli.config import CACHE_DIR, FLARESOLVERR_URL, SHINDEN_BASE

_CF_CACHE = CACHE_DIR / "cf_clearance.json"
_EXPIRY_MARGIN = 300  # refresh 5 min before cookie expiry


def _load_cached() -> tuple[str, str] | None:
    """Return (cf_clearance, user_agent) from disk if still valid, else None."""
    try:
        data = json.loads(_CF_CACHE.read_text(encoding="utf-8"))
        if time.time() < data.get("expires", 0) - _EXPIRY_MARGIN:
            return data["cf_clearance"], data["user_agent"]
    except Exception:  # nosec B110
        pass
    return None


def _save_cached(cf_clearance: str, user_agent: str, expires: float) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CF_CACHE.write_text(
            json.dumps({"cf_clearance": cf_clearance, "user_agent": user_agent, "expires": expires}),
            encoding="utf-8",
        )
    except Exception:  # nosec B110
        pass


def _fetch_via_flaresolverr() -> tuple[str, str] | None:
    """POST to FlareSolverr to solve the JS challenge; return (cf_clearance, user_agent) or None.

    FlareSolverr runs a headless Chrome that solves the Cloudflare challenge
    and returns the resulting cookies including cf_clearance.

    urllib.request is used intentionally: FlareSolverr is local infrastructure
    that does not sit behind Cloudflare, so TLS impersonation via curl_cffi is
    unnecessary and stdlib is sufficient.
    """
    try:
        payload = json.dumps(
            {"cmd": "request.get", "url": f"{SHINDEN_BASE}/", "maxTimeout": 60000}
        ).encode()
        req = urllib.request.Request(
            f"{FLARESOLVERR_URL}/v1",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=70) as resp:  # nosec B310
            data = json.loads(resp.read())
    except Exception:
        return None

    if data.get("status") != "ok":
        return None

    sol = data.get("solution", {})
    user_agent = sol.get("userAgent", "")
    for cookie in sol.get("cookies", []):
        if cookie.get("name") == "cf_clearance":
            value = cookie["value"]
            expires = float(cookie.get("expires", -1))
            if expires < 0:
                expires = time.time() + 3600
            _save_cached(value, user_agent, expires)
            return value, user_agent
    return None


def _get_clearance() -> tuple[str, str] | None:
    """Return cached clearance if valid, otherwise resolve via FlareSolverr."""
    cached = _load_cached()
    if cached is not None:
        return cached
    if not FLARESOLVERR_URL:
        return None
    # http.py is a leaf module below ui/; importing progress here would create a
    # circular dependency chain, so stderr print is the intentional alternative.
    print("Solving Cloudflare challenge via FlareSolverr (takes ~20-30 s)...", file=sys.stderr, flush=True)
    return _fetch_via_flaresolverr()


def make_client() -> cffi_requests.Session:
    """Build a curl_cffi Session that bypasses Cloudflare on shinden.pl.

    Strategy:
    1. Use cached cf_clearance if still valid (fast path).
    2. Otherwise resolve via FlareSolverr (slow, ~20-30 s, result cached).
    3. Without FlareSolverr configured, use impersonate= only (may get 403).
    """
    clearance = _get_clearance()

    headers: dict[str, str] = {
        "Referer": f"{SHINDEN_BASE}/",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    # _rnd is a Unix timestamp shinden uses as a lightweight session seed;
    # it must be present from the very first request.
    cookies: dict[str, str] = {"_rnd": str(int(time.time()))}

    if clearance is not None:
        cf_clearance, user_agent = clearance
        cookies["cf_clearance"] = cf_clearance
        if user_agent:
            headers["User-Agent"] = user_agent

    return cffi_requests.Session(
        impersonate="chrome",
        headers=headers,
        cookies=cookies,
        timeout=30.0,
        allow_redirects=True,
    )
