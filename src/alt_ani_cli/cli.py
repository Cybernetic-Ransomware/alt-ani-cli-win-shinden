"""alt-ani-cli — Pythonowy klient shinden.pl dla Windows PowerShell."""

from __future__ import annotations

import argparse
import contextlib
import sys

from alt_ani_cli import __version__, download, extract, history
from alt_ani_cli.content import CONTENT
from alt_ani_cli.errors import (
    AntiBotError,
    NoStreamError,
    ParseError,
    PlayerNotFoundError,
    ShindenError,
)
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.player import runner as player_runner
from alt_ani_cli.shinden import api as shinden_api
from alt_ani_cli.shinden import episode as shinden_episode
from alt_ani_cli.shinden import http as shinden_http
from alt_ani_cli.shinden import search as shinden_search
from alt_ani_cli.shinden import series as shinden_series
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesRef
from alt_ani_cli.ui import progress

_C = CONTENT
_CLI = _C["cli"]
_PROG = _C["progress"]


def _build_parser() -> argparse.ArgumentParser:
    h = _CLI["help"]
    p = argparse.ArgumentParser(
        prog="alt-ani-cli",
        description=_CLI["description"],
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=_CLI["epilog"],
    )

    p.add_argument("query", nargs="*", metavar="QUERY", help=h["query"])

    p.add_argument("--url", metavar="URL", help=h["url"])

    p.add_argument("-c", "--continue", dest="resume", action="store_true", help=h["resume"])
    p.add_argument("-d", "--download", action="store_true", help=h["download"])
    p.add_argument("-D", "--delete-history", action="store_true", help=h["delete_history"])

    p.add_argument("-e", "--episode", metavar="RANGE", help=h["episode"])
    p.add_argument("-q", "--quality", default=None, metavar="QUALITY", help=h["quality"])
    p.add_argument("-S", "--select-nth", type=int, metavar="N", help=h["select_nth"])

    p.add_argument("-v", "--vlc", action="store_true", help=h["vlc"])
    p.add_argument("--no-detach", action="store_true", help=h["no_detach"])
    p.add_argument("--debug", action="store_true", help=h["debug"])

    p.add_argument("--player-name", metavar="NAME", help=h["player_name"])
    p.add_argument("--lang", metavar="{pl,jp,en}", help=h["lang"])
    p.add_argument("--subs", metavar="{pl,en,none}", help=h["subs"])

    p.add_argument("--cookies-file", metavar="PATH", help=h["cookies_file"])
    p.add_argument("--cookies-browser", metavar="{chrome,firefox,edge,opera,brave}", help=h["cookies_browser"])

    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    return p


def _parse_range(range_str: str, episodes: list[EpisodeRow]) -> list[EpisodeRow]:
    """Convert range string to an ordered list of EpisodeRow objects.

    Supported formats:
      "5"     → single episode 5
      "1-5"   → episodes 1 through 5 (inclusive)
      "-1"    → last available episode
      "1 5 7" → episodes 1, 5 and 7 (space-separated)
    """
    s = range_str.strip()

    if s == "-1":
        return [episodes[-1]] if episodes else []

    # Range: start-end (but not a negative number — those are handled above)
    if "-" in s and not s.startswith("-"):
        parts = s.split("-", 1)
        try:
            start, end = float(parts[0]), float(parts[1])
            return [ep for ep in episodes if start <= ep.number <= end]
        except ValueError:
            pass

    # Space-separated multi-select
    if " " in s:
        targets = set()
        for p in s.split():
            with contextlib.suppress(ValueError):
                targets.add(float(p))
        return [ep for ep in episodes if ep.number in targets]

    # Single episode
    try:
        num = float(s)
        return [ep for ep in episodes if ep.number == num]
    except ValueError:
        pass

    progress.warn(_PROG["range_unrecognized"].format(range=repr(range_str)))
    return []


def _filter_players(
    players: list[PlayerEntry],
    *,
    lang: str | None,
    subs: str | None,
    player_name: str | None,
) -> list[PlayerEntry]:
    result = players[:]
    if player_name:
        filtered = [p for p in result if p.player.lower() == player_name.lower()]
        if filtered:
            result = filtered
    if lang:
        filtered = [p for p in result if p.lang_audio == lang]
        if filtered:
            result = filtered
    if subs:
        filtered = [p for p in result if p.lang_subs == subs]
        if filtered:
            result = filtered
    return result or players  # fallback to full list if no match


def _pick_quality(stream: Stream, quality: str) -> Stream:
    if not stream.qualities:
        return stream

    def _h(key: str) -> float:
        try:
            return float(key.rstrip("p"))
        except ValueError:
            return 0.0

    if quality == "best":
        url = max(stream.qualities.items(), key=lambda kv: _h(kv[0]))[1]
    elif quality == "worst":
        url = min(stream.qualities.items(), key=lambda kv: _h(kv[0]))[1]
    else:
        url = stream.qualities.get(quality, stream.url)

    return Stream(url=url, headers=stream.headers, qualities=stream.qualities, ext=stream.ext)


def _resolve_with_fallback(
    client,
    players: list[PlayerEntry],
    chosen: PlayerEntry,
    auto: bool,
    ep_number: float,
    *,
    cookies_file: str | None = None,
    cookies_browser: str | None = None,
):
    """Try chosen player; if it fails and auto-mode is active, walk down the sorted list.

    Returns (Stream, EmbedURL) on success, (None, None) when all players fail.
    For user-chosen players (auto=False) only the selected player is attempted.
    """
    candidates = players if auto else [chosen]
    # Always start with the explicitly chosen player
    if auto and chosen in candidates:
        candidates = [chosen] + [p for p in candidates if p is not chosen]

    for candidate in candidates:
        try:
            with progress.spinner(_PROG["spinner_api"].format(label=ANTIBOT_LABEL)):
                embed = shinden_api.resolve_embed(client, candidate.online_id)
            _url_short = embed.url if len(embed.url) <= 80 else embed.url[:77] + "…"
            progress.info(_PROG["embed"].format(url=_url_short))
            stream = extract.resolve(
                embed.url,
                embed.referer,
                cookies_file=cookies_file,
                cookies_browser=cookies_browser,
            )
            return stream, embed
        except (NoStreamError, AntiBotError) as exc:
            progress.warn(_PROG["player_failed_long"].format(player=repr(candidate.player), number=ep_number, exc=exc))

    return None, None


def _setup_encoding() -> None:
    """Reconfigure stdout/stderr to UTF-8 before first Rich Console use."""
    import sys as _sys
    from io import TextIOWrapper

    if isinstance(_sys.stdout, TextIOWrapper):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(_sys.stderr, TextIOWrapper):
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if _sys.platform == "win32":
        import colorama

        colorama.just_fix_windows_console()


ANTIBOT_LABEL = "5 s antibot delay"


def _print_debug(stream: Stream, embed) -> None:
    from rich.table import Table

    from alt_ani_cli.ui.progress import _get

    con = _get()
    con.print(f"\n[bold]Embed URL:[/bold] {embed.url}")
    con.print(f"[bold]Direct URL:[/bold] {stream.url}")
    con.print(f"[bold]Ext:[/bold] {stream.ext}")

    if stream.headers:
        t = Table("Header", "Value", title="HTTP headers")
        for k, v in stream.headers.items():
            t.add_row(k, v)
        con.print(t)

    if stream.qualities:
        t = Table("Quality", "URL", title="Available qualities")
        for q, u in sorted(stream.qualities.items(), key=lambda kv: kv[0]):
            t.add_row(q, u[:80] + "..." if len(u) > 80 else u)
        con.print(t)


def _run_noninteractive(args, client) -> None:  # noqa: C901
    if not args.query and not args.url and not args.resume:
        import argparse as _ap

        _ap.ArgumentParser(prog="alt-ani-cli").error(_CLI["errors"]["missing_input"])

    if args.resume:
        all_entries = history.list_all()
        if not all_entries:
            progress.error(_PROG["history_empty"])
            sys.exit(1)
        ref, last_ep = all_entries[0]
    elif args.url:
        ref = shinden_series.parse_series_url(args.url)
        last_ep = 0.0
    else:
        query = " ".join(args.query)
        progress.info(_PROG["searching"].format(query=repr(query)))
        hits = shinden_search.search_series(client, query)
        if not hits:
            progress.error(_PROG["no_results"].format(query=repr(query)))
            sys.exit(1)

        idx = (args.select_nth - 1) if args.select_nth else 0
        if idx < 0 or idx >= len(hits):
            progress.error(_PROG["select_nth_invalid"].format(n=args.select_nth, count=len(hits)))
            sys.exit(1)
        ref = shinden_series.parse_series_url(hits[idx].url)
        ref = SeriesRef(id=ref.id, slug=ref.slug, title=hits[idx].title, url=ref.url)
        last_ep = 0.0

    progress.info(_PROG["fetching_episodes"].format(title=ref.title))
    ref, episodes = shinden_series.list_episodes(client, ref)

    if not episodes:
        progress.error(_PROG["no_episodes"])
        sys.exit(1)

    if args.episode:
        targets = _parse_range(args.episode, episodes)
        if not targets:
            progress.error(_PROG["range_not_found"].format(range=repr(args.episode)))
            sys.exit(1)
    elif args.resume and last_ep > 0:
        remaining = [ep for ep in episodes if ep.number > last_ep]
        if not remaining:
            progress.warn(_PROG["watched_all"].format(title=ref.title))
            remaining = episodes
        targets = [remaining[0]]
    else:
        targets = [episodes[0]]

    player_kind = "vlc" if args.vlc else "mpv"
    _episode_action: str | None = None

    for ep in targets:
        progress.info(_PROG["episode"].format(number=ep.number, title=ep.title))

        ep_resp = client.get(ep.url)
        ep_resp.raise_for_status()
        players = shinden_episode.sort_players(
            shinden_episode.parse_players(ep_resp.text),
            download=args.download,
        )

        if not players:
            progress.warn(_PROG["no_players"].format(number=ep.number))
            continue

        players = _filter_players(
            players,
            lang=args.lang,
            subs=args.subs,
            player_name=args.player_name,
        )

        chosen = players[0]
        stream, embed = _resolve_with_fallback(
            client,
            players,
            chosen,
            auto=True,
            ep_number=ep.number,
            cookies_file=args.cookies_file,
            cookies_browser=args.cookies_browser,
        )

        if stream is None:
            progress.warn(_PROG["no_player_worked"].format(number=ep.number))
            continue

        quality = args.quality or "best"
        stream = _pick_quality(stream, quality)
        title = f"{ref.title} — Odcinek {ep.number:g}"

        if args.download:
            _action = "download"
        elif args.debug:
            _action = "debug"
        else:
            _action = _episode_action or "play"

        if _action == "download":
            download.run(stream, ep, ref)
        elif _action == "debug":
            _print_debug(stream, embed)
        else:
            player_runner.play(stream, kind=player_kind, title=title, no_detach=args.no_detach)
            progress.success(_PROG["playing"].format(kind=player_kind, title=title))

        history.upsert(ref, last_ep=ep.number)


def _run_interactive(args, client) -> None:
    from alt_ani_cli.flow.handlers import HANDLERS
    from alt_ani_cli.flow.state import _VIRTUAL_SCREENS, FlowState, Screen, _BackSentinel

    state = FlowState(args=args, client=client, quality=args.quality)

    history_stack: list[Screen] = []
    screen: Screen | None = Screen.START_MODE

    while screen is not None:
        result = HANDLERS[screen](state)
        if isinstance(result, _BackSentinel):
            screen = None if not history_stack else history_stack.pop()
        elif result is None:
            screen = None
        else:
            if screen not in _VIRTUAL_SCREENS:
                history_stack.append(screen)
            screen = result


def main() -> None:  # noqa: C901
    _setup_encoding()
    parser = _build_parser()
    args = parser.parse_args()

    if args.delete_history:
        history.clear()
        progress.success(_PROG["history_cleared"])
        sys.exit(0)

    client = shinden_http.make_client()
    interactive = sys.stdin.isatty() and not args.select_nth

    try:
        if interactive:
            _run_interactive(args, client)
        else:
            _run_noninteractive(args, client)
    except KeyboardInterrupt:
        progress.warn(_PROG["interrupted"])
        sys.exit(130)
    except (AntiBotError, NoStreamError, ParseError) as exc:
        progress.error(str(exc))
        sys.exit(1)
    except PlayerNotFoundError as exc:
        progress.error(str(exc))
        sys.exit(1)
    except ShindenError as exc:
        progress.error(_PROG["shinden_error"].format(exc=exc))
        sys.exit(1)
    finally:
        client.close()
