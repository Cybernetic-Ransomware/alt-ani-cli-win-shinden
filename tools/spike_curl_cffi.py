"""Verify that the Cloudflare bypass strategy works for shinden.pl.

Run with:  uv run python tools/spike_curl_cffi.py

Tests two paths in order:
  1. curl_cffi impersonate="chrome" alone (no FlareSolverr)
  2. FlareSolverr + cf_clearance cookie (if ALT_ANI_CLI_FLARESOLVERR_URL is set)

PASS criteria per URL:
  - status 200
  - body does NOT contain Cloudflare challenge markers
  - body contains expected HTML structure
"""
import os
import sys

sys.path.insert(0, "src")

from curl_cffi import requests as cffi

from alt_ani_cli.shinden.http import _get_clearance

SEARCH_URL = "https://shinden.pl/series?q=naruto"
EPISODES_URL = "https://shinden.pl/series/71348-super-no-ura-de-yani-suu-futari/all-episodes"

CHALLENGE_PAGE_MARKERS = ("Just a moment", "<title>Just a moment...</title>")
SEARCH_OK = "div-row"
EPISODES_OK = "list-episode-checkboxes"


def check(label: str, url: str, ok_marker: str, session: cffi.Session) -> bool:
    print(f"\n[{label}] GET {url}")
    r = session.get(url)
    print(f"  status: {r.status_code}")
    # Cloudflare embeds its scripts (cdn-cgi/challenge) even in pages it serves
    # normally. The real indicator of a blocked page is the challenge title or
    # absence of expected page content.
    challenge_page = any(m in r.text for m in CHALLENGE_PAGE_MARKERS)
    has_content = ok_marker in r.text
    print(f"  challenge page: {challenge_page}")
    print(f"  expected marker '{ok_marker}' found: {has_content}")
    return r.status_code == 200 and not challenge_page and has_content


print("=== Path 1: curl_cffi impersonate only ===")
with cffi.Session(impersonate="chrome") as s:
    r1a = check("search", SEARCH_URL, SEARCH_OK, s)
    r1b = check("episodes", EPISODES_URL, EPISODES_OK, s)

if r1a and r1b:
    print("\nPASS — curl_cffi TLS impersonation alone bypasses Cloudflare.")
    sys.exit(0)

print("\nFAIL — TLS impersonation blocked. Testing FlareSolverr path...")

flaresolverr_url = os.getenv("ALT_ANI_CLI_FLARESOLVERR_URL", "")
if not flaresolverr_url:
    print("\nALT_ANI_CLI_FLARESOLVERR_URL not set.")
    print("Set it to http://localhost:8191 and run FlareSolverr before retesting.")
    sys.exit(1)

print(f"\n=== Path 2: FlareSolverr ({flaresolverr_url}) ===")
clearance = _get_clearance()
if clearance is None:
    print("FlareSolverr did not return cf_clearance. Check that FlareSolverr is running.")
    sys.exit(1)

cf_clearance, user_agent = clearance
print(f"cf_clearance obtained (UA: {user_agent[:60]}...)")

cookies = {"cf_clearance": cf_clearance}
headers = {"User-Agent": user_agent} if user_agent else {}

with cffi.Session(impersonate="chrome", cookies=cookies, headers=headers) as s:
    r2a = check("search", SEARCH_URL, SEARCH_OK, s)
    r2b = check("episodes", EPISODES_URL, EPISODES_OK, s)

print()
if r2a and r2b:
    print("PASS — FlareSolverr + cf_clearance bypasses Cloudflare. Full migration can proceed.")
    sys.exit(0)
else:
    print("FAIL — FlareSolverr clearance obtained but requests still blocked.")
    print("The IP may have changed or cf_clearance is bound to a different User-Agent.")
    sys.exit(1)
