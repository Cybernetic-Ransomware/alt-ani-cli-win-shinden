# alt-ani-cli-win

![Python](https://img.shields.io/badge/python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-FF0000?style=for-the-badge&logo=youtube&logoColor=white)
![Rich](https://img.shields.io/badge/Rich-FAD000?style=for-the-badge&logo=python&logoColor=black)
![InquirerPy](https://img.shields.io/badge/InquirerPy-4B8BBE?style=for-the-badge&logo=python&logoColor=white)
![Ruff](https://img.shields.io/badge/Ruff-FCC21B?style=for-the-badge&logo=ruff&logoColor=black)
![Pytest](https://img.shields.io/badge/pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)
![UV](https://img.shields.io/badge/UV-DE5FE9?style=for-the-badge&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-EUPL--1.2-003399?style=for-the-badge)

Python CLI for watching and downloading anime from [shinden.pl](https://shinden.pl), optimised for Windows (PowerShell / Windows Terminal).

## Requirements

- Python 3.14+ managed by [uv](https://docs.astral.sh/uv/)
- [mpv](https://mpv.io/) or [mpv.net](https://github.com/mpvnet-player/mpv.net) for playback
  ```powershell
  winget install mpv.net   # or: scoop install mpv
  ```
- [ffmpeg](https://ffmpeg.org/) (optional) — enables HLS merging during downloads
  ```powershell
  winget install ffmpeg    # or: scoop install ffmpeg
  ```
- [Docker](https://www.docker.com/) — required to run FlareSolverr (see below)

## Cloudflare bypass setup

Shinden.pl is protected by Cloudflare bot detection. The app uses
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — a headless Chrome proxy — to
solve the JS challenge and obtain a `cf_clearance` cookie.

**Option A — FlareSolverr on the local machine:**

```powershell
docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191 ghcr.io/flaresolverr/flaresolverr

[System.Environment]::SetEnvironmentVariable("ALT_ANI_CLI_FLARESOLVERR_URL", "http://localhost:8191", "User")
```

**Option B — FlareSolverr on a home server (no Docker on the PC):**

```powershell
# Deploy once on the server
ssh user@192.168.0.x "docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191 ghcr.io/flaresolverr/flaresolverr"

# Point the app to it (permanent, survives reboots)
[System.Environment]::SetEnvironmentVariable("ALT_ANI_CLI_FLARESOLVERR_URL", "http://192.168.0.x:8191", "User")
```

The `cf_clearance` cookie is cached locally and reused until it expires (~1-2 hours).
Only the first request after expiry is slow (~20 s); all subsequent ones are instant.

## Installation

```powershell
git clone https://github.com/Cybernetic-Ransomware/alt-ani-cli-win-shinden
cd alt-ani-cli-win-shinden
uv sync --no-group dev
```

## Usage

```
alt-ani-cli [OPTIONS] [QUERY...]
```

Run without arguments for an interactive wizard (search → select series → pick episodes → choose player → quality → action).

### Flags

| Flag | Description |
|------|-------------|
| `QUERY` | Title to search on shinden.pl |
| `--url URL` | Skip search — use a direct series URL |
| `-c`, `--continue` | Resume from watch history |
| `-d`, `--download` | Download instead of playing |
| `-D`, `--delete-history` | Clear watch history and exit |
| `-e RANGE` | Episode number or range: `5`, `1-5`, `-1` (last), `1 5 7` |
| `-q QUALITY` | Quality: `best`, `worst`, `1080p`, `720p` … (default: interactive menu) |
| `-S N` | Auto-select N-th search result (1-based, skips menus) |
| `-v`, `--vlc` | Use VLC instead of mpv |
| `--no-detach` | Run player in foreground (blocks terminal) |
| `--debug` | Print stream URLs, do not launch player |
| `--player-name NAME` | Filter by player name (`CDA`, `Mp4upload`, …) |
| `--lang {pl,jp,en}` | Filter by audio language |
| `--subs {pl,en,none}` | Filter by subtitle language |
| `--allow-fallback` | When `--lang`/`--subs`/`--player-name` match nothing, use the full player list instead of failing |
| `--show-sources` | Interactive mode only — resolve and show each player's real host in the picker (slow: ~7 s per player due to antibot delay) |
| `--cookies-file PATH` | Netscape cookies file (for age-gated content) |
| `--cookies-browser NAME` | Extract cookies from browser (`chrome`, `firefox`, …) |

### Examples

```powershell
# Interactive wizard
alt-ani-cli

# Search and pick interactively
alt-ani-cli fate strange fake

# Auto-select first result, watch episode 1
alt-ani-cli -S 1 -e 1 soul eater

# Watch a specific series by URL
alt-ani-cli --url https://shinden.pl/series/65137-fate-strange-fake

# Download episodes 1–3 in 720p
alt-ani-cli -d -e 1-3 -q 720p --url https://shinden.pl/series/65137-fate-strange-fake

# Resume from history
alt-ani-cli -c

# Polish dub only
alt-ani-cli --lang pl vinland saga

# Japanese audio + Polish subtitles
alt-ani-cli --lang jp --subs pl berserk

# CDA with age-gate — use browser cookies
alt-ani-cli --cookies-browser chrome --url https://shinden.pl/series/...
```

### Resume from history

When continuing a series, the episode picker opens on the first unwatched episode and
shows only the unwatched ones. Watched episodes stay reachable — scrolling up reveals
them one by one (marked with ✓), growing the list up to 15 rows, after which it scrolls
as a sliding window.

### Player selection

Players are sorted automatically:

- **Watch mode**: PL audio → JP → EN; highest resolution first
- **Download mode**: JP audio → EN → PL; highest resolution first

Failed players are marked with `✗` and can be retried interactively.

### Environment variables

| Variable | Description |
|----------|-------------|
| `ALT_ANI_CLI_FLARESOLVERR_URL` | FlareSolverr URL for Cloudflare bypass (e.g. `http://localhost:8191`) |
| `ANI_CLI_PLAYER` | Full path to mpv/mpvnet executable |
| `ALT_ANI_CLI_ANTIBOT_DELAY` | Seconds to wait between API calls (default: `5.0`) |

## Downloads

Files are saved to `%USERPROFILE%\Videos\alt-ani-cli\` by default.

## Development

One-time setup:

```powershell
uv sync                       # install all dependencies
uv run pre-commit install     # install git hooks
```

Day-to-day with [just](https://github.com/casey/just):

```powershell
just test      # run tests
just lint      # ruff check, ty, codespell, bandit
just format    # ruff format --check (report only)
```

## Supported video hosts

Native extractors: mp4upload, streamtape, dood, Lycoris Cafe, streamwish/filemoon family (JWPlayer), CDA, sibnet, VK.

Lycoris Cafe embeds are resolved through the host API and expose the available direct qualities (`1080p`, `720p`, `480p`) plus `source-mkv` when the API provides it.

All other hosts fall back to yt-dlp (1500+ supported sites).

**Not supported: mega.nz.** MEGA serves end-to-end encrypted files (AES-128-CTR, decryption key
in the URL fragment), so this project cannot hand the stream directly to yt-dlp or mpv without a
custom decrypting proxy — the host fails fast with a clear message instead of attempting extraction.

> **TODO — full MEGA player support**: call the MEGA API for the direct (encrypted) file URL,
> decrypt AES-128-CTR on the fly through a local streaming proxy for mpv, and add a custom
> download path bypassing yt-dlp. Requires a new crypto dependency (e.g. `pycryptodome`).

## Security notes

**`GUEST_AUTH` token** — the value hardcoded in `config.py` is a public guest token issued by shinden.pl for unauthenticated API access.

It encodes the literal string `_guest_:0,5,21000000,255,4174293644` in Base64. Any visitor to shinden.pl receives the same token, so it is safe to commit and share openly.

## Developer tools

Scripts in `tools/` are used during development to inspect the shinden.pl API. They are not required for normal use.

| Script | Purpose |
|--------|---------|
| `tools/spike_curl_cffi.py` | Verifies the Cloudflare bypass chain: curl_cffi TLS → FlareSolverr |
| `tools/debug_embed.py <url>` | Scans a player JS bundle for CDN domains and HLS/token patterns — used to reverse-engineer new embed hosts |
| `tools/dump_search_html.py [query]` | Dumps parsed search result rows from shinden.pl — used to debug the search HTML parser |

```powershell
uv run python tools/spike_curl_cffi.py
uv run python tools/dump_search_html.py "soul eater"
uv run python tools/debug_embed.py https://example-embed-host.com/e/abc123
```
