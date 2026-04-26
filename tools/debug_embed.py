"""Find CDN domain and HLS token pattern in Byse bundle."""
import sys, re
sys.path.insert(0, "src")

import httpx
from alt_ani_cli.config import USER_AGENT

embed_url = sys.argv[1]
base = embed_url.split("/e/")[0]
file_id = embed_url.rstrip("/").split("/")[-1]

headers = {"Referer": "https://shinden.pl/", "User-Agent": USER_AGENT, "Accept": "*/*"}

with httpx.Client(follow_redirects=True, timeout=60.0) as client:
    shell = client.get(embed_url, headers=headers)
    bundle_url = re.search(r'src="(/assets/index-[^"]+\.js)"', shell.text).group(1)
    js = client.get(base + bundle_url, headers=headers).text

# Find all domain-like strings (not localhost, not common libs)
print("--- External domains in bundle ---")
domains = re.findall(r'["\`]((?:https?://)?(?:[a-z0-9-]+\.)+(?:com|io|net|tv|stream|cdn|media|video)[a-z0-9/-]*)["\`]', js)
freq: dict[str, int] = {}
for d in domains:
    if any(skip in d for skip in ["mozilla","w3c","example","schema","jquery","google","github","webpack","react","eslint"]):
        continue
    freq[d] = freq.get(d, 0) + 1

for d, c in sorted(freq.items(), key=lambda x: -x[1])[:30]:
    print(f"  {c:3d}x  {d}")

# Find HLS/manifest patterns
print("\n--- HLS/manifest/token patterns ---")
for pat in [
    r'\.m3u8',
    r'master\.(?:m3u8|mpd)',
    r'playlist',
    r'manifest',
    r'hls\.',
    r'videoToken',
    r'accessToken',
    r'signed',
    r'cdn\.',
]:
    positions = [m.start() for m in re.finditer(pat, js, re.IGNORECASE)]
    if positions:
        pos = positions[0]
        snippet = js[max(0,pos-100):pos+150].replace("\n"," ")
        print(f"\n  [{pat}] ({len(positions)} hits): ...{snippet}...")
