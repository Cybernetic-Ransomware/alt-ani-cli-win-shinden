# alt-ani-cli-win

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

## Installation

```powershell
git clone https://github.com/Cybernetic-Ransomware/alt-ani-cli-win
cd alt-ani-cli-win
uv sync
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

### Player selection

Players are sorted automatically:

- **Watch mode**: PL audio → JP → EN; highest resolution first
- **Download mode**: JP audio → EN → PL; highest resolution first

Failed players are marked with `✗` and can be retried interactively.

### Environment variables

| Variable | Description |
|----------|-------------|
| `ANI_CLI_PLAYER` | Full path to mpv/mpvnet executable |
| `ALT_ANI_CLI_ANTIBOT_DELAY` | Seconds to wait between API calls (default: `5.0`) |

## Downloads

Files are saved to `%USERPROFILE%\Videos\alt-ani-cli\` by default.

## Development

```powershell
uv sync
uv run pytest -v
uv run ruff check src
```

## Supported video hosts

Native extractors: mp4upload, streamtape, dood, streamwish/filemoon family (JWPlayer), CDA, sibnet, VK.  
All other hosts fall back to yt-dlp (1500+ supported sites).
