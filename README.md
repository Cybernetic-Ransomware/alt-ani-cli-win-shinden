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
- [ffmpeg](https://ffmpeg.org/) (optional) — yt-dlp downloads HLS natively; when ffmpeg is available it remuxes
  the result into a proper MP4. Without it, an HLS download stays an MPEG-TS stream despite the `.mp4` extension.
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
| `-c`, `--continue` | Resume from watch history (with `-d`: resume from download history — first episode not yet downloaded) |
| `-d`, `--download` | Download instead of playing |
| `-D`, `--delete-history` | Clear watch and download history and exit |
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

# Download the next missing episode of the most recently downloaded series
alt-ani-cli -c -d

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
| `ALT_ANI_CLI_XHR_BACKEND` | Player API backend: `new` (shinden.pl/xhr with the episode page token, default) or `api4` (legacy guest endpoint) |

## Downloads

Files are saved to `%USERPROFILE%\Videos\alt-ani-cli\` by default.

## History

Watch and download history is kept in a local SQLite file, `history.db`, in the application's data directory.
A `history.json` written by an earlier version is imported once on the first start: the JSON file is only read,
a `history.json.pre-sqlite.bak` copy is placed next to it, and entries that cannot be read are skipped.
`-D` clears the history without bringing the old file back. Details are in
[ADR-0002](docs/adr/0002-core-cli-split.md#history-migration).

The same file also holds state prepared for the Android client (watch progress, a download queue and an offline
library; [ADR-0004](docs/adr/0004-local-state-model.md)). `-D` does not remove it. When a database from version
0.3.x is upgraded, a verified copy `history.db.schema-1.<time>.bak` is written next to it first; an older version
refuses the upgraded file, and restoring that copy is the way back.

## Development

For branching, integration, and release rules, see [ADR-0001](docs/adr/0001-branching-and-release-flow.md).

### Project layout

A uv workspace; architecture and layer rules are described in [ADR-0002](docs/adr/0002-core-cli-split.md).

| Path | What it is |
|------|------------|
| `packages/core` | `alt-ani-core` — the engine: Shinden integration, extractors, downloads, history. No terminal, no desktop paths |
| `apps/cli` | `alt-ani-cli-win` — this CLI: arguments, menus, mpv/VLC, paths, Polish texts |
| `apps/android` | Feasibility spike and `alt_ani_bridge`, a JSON facade over the engine ([ADR-0003](docs/adr/0003-android-feasibility.md)); not built by CI |
| `tests` | One suite for the workspace, including `tests/architecture` (layer rules) and `tests/bridge` |

One-time setup:

```powershell
uv sync                       # install all dependencies
uv run pre-commit install     # install git hooks
```

Day-to-day with [just](https://github.com/casey/just):

```powershell
just test           # run tests
just lint           # ruff check + format check, ty, import contracts, codespell, bandit
just format         # apply ruff formatting
just package        # build both wheels and check them in clean virtual environments
just replay-failures "PATH_TO_LOG"  # replay resolve_result failures from a diagnostics session log
```

## Supported video hosts

Native extractors: mp4upload, streamtape, dood, Lycoris Cafe, Vidara/Viewdara, Playmate, Uqload, Flyf,
streamwish/filemoon/morencius family (JWPlayer), CDA, sibnet, VK.

Lycoris Cafe embeds are resolved through the host API and expose the available direct qualities (`1080p`, `720p`, `480p`) plus `source-mkv` when the API provides it.

All other hosts fall back to yt-dlp (1500+ supported sites).

**Not supported: mega.nz.** MEGA serves end-to-end encrypted files (AES-128-CTR, decryption key
in the URL fragment), so this project cannot hand the stream directly to yt-dlp or mpv without a
custom decrypting proxy — the host fails fast with a clear message instead of attempting extraction.

> **TODO — full MEGA player support**: call the MEGA API for the direct (encrypted) file URL,
> decrypt AES-128-CTR on the fly through a local streaming proxy for mpv, and add a custom
> download path bypassing yt-dlp. Requires a new crypto dependency (e.g. `pycryptodome`).

## Security notes

**`GUEST_AUTH` token** — the value hardcoded in `alt_ani_core/config.py` is a public guest token issued by shinden.pl for unauthenticated API access.

It encodes the literal string `_guest_:0,5,21000000,255,4174293644` in Base64. Any visitor to shinden.pl receives the same token, so it is safe to commit and share openly.

**Episode page token** — the default player API uses the short-lived token each episode page carries. It is kept in memory for that episode only and is never written to history, caches or the diagnostics log.

## Developer tools

Scripts in `tools/` are used during development to inspect the shinden.pl API. They are not required for normal use.

| Script | Purpose |
|--------|---------|
| `tools/debug_embed.py <url>` | Scans a player JS bundle for CDN domains and HLS/token patterns — used to reverse-engineer new embed hosts |
| `tools/replay_failed_resolvers.py <log>` | Replays `resolve_result ok=False` entries from a diagnostics session log against live hosts, showing a BEFORE/NOW comparison per case — also available as `just replay-failures <log>` |
| `tools/check_packaging.py` | Builds both wheels and checks entry points, packaged content and a terminal-free engine in clean virtual environments — also `just package` |

```powershell
uv run python tools/debug_embed.py https://example-embed-host.com/e/abc123
just replay-failures "$env:LOCALAPPDATA\alt-ani-cli\alt-ani-cli\Cache\diagnostics\session_20260919_232825_752907.log"
```
