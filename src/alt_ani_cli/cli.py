"""alt-ani-cli — Pythonowy klient shinden.pl dla Windows PowerShell."""

from __future__ import annotations

import argparse
import contextlib
import sys

from alt_ani_cli import __version__, download, extract, history
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
from alt_ani_cli.ui import menus, progress


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alt-ani-cli",
        description="Oglądaj anime z shinden.pl bezpośrednio w terminalu.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Przyklady:\n"
            "  alt-ani-cli fate strange fake            interaktywne wyszukiwanie\n"
            "  alt-ani-cli -S 1 -e 1-5 fate strange    odcinki 1-5, pierwszy wynik\n"
            "  alt-ani-cli -S 1 -e -1 soul eater        ostatni odcinek\n"
            "  alt-ani-cli --url https://shinden.pl/series/65137-fate-strange-fake -e 3\n"
            "  alt-ani-cli -c                           kontynuuj z historii\n"
            "  alt-ani-cli -d -e 3 soul eater           pobierz odcinek 3\n"
            "  alt-ani-cli --lang pl -e 1 vinland saga  tylko polskie audio\n"
            "  alt-ani-cli --lang jp --subs pl -S 1 -e 1 berserk\n"
            "\n"
            "Filtrowanie jezyka: --lang pl (dubbing PL), --lang jp --subs pl (sub PL)\n"
            'Zakres odcinkow:    "5" (jeden), "1-5" (zakres), "-1" (ostatni), "1 5 7" (lista)\n'
        ),
    )

    p.add_argument("query", nargs="*", metavar="QUERY", help="Tytuł do wyszukania na shinden.pl (pomijany gdy --url lub -c).")

    p.add_argument("--url", metavar="URL", help="Bezpośredni URL serii (https://shinden.pl/series/...).")

    p.add_argument("-c", "--continue", dest="resume", action="store_true", help="Kontynuuj oglądanie z historii.")
    p.add_argument("-d", "--download", action="store_true", help="Pobierz odcinek zamiast odtwarzać.")
    p.add_argument("-D", "--delete-history", action="store_true", help="Wyczyść historię i wyjdź.")

    p.add_argument("-e", "--episode", metavar="RANGE", help='Numer odcinka lub zakres: "5", "1-5", "-1" (ostatni).')
    p.add_argument(
        "-q",
        "--quality",
        default=None,
        metavar="QUALITY",
        help='Preferowana jakość: "best", "worst", "1080p", "720p" … (domyślnie: menu interaktywne lub "best").',
    )
    p.add_argument(
        "-S", "--select-nth", type=int, metavar="N", help="Automatycznie wybierz N-ty wynik wyszukiwania (1-bazowy)."
    )

    p.add_argument("-v", "--vlc", action="store_true", help="Użyj vlc.exe zamiast mpv.exe.")
    p.add_argument("--no-detach", action="store_true", help="Uruchom player na pierwszym planie (blokuje terminal).")
    p.add_argument("--debug", action="store_true", help="Wypisz bezpośrednie linki wideo, nie uruchamiaj playera.")

    p.add_argument("--player-name", metavar="NAME", help='Filtruj po nazwie playera: "CDA", "Mp4upload", "Sibnet" …')
    p.add_argument("--lang", metavar="{pl,jp,en}", help="Filtruj po języku audio (lang_audio).")
    p.add_argument("--subs", metavar="{pl,en,none}", help="Filtruj po języku napisów (lang_subs).")

    p.add_argument(
        "--cookies-file",
        metavar="PATH",
        help="Plik ciasteczek w formacie Netscape (eksportuj np. rozszerzeniem 'Get cookies.txt').",
    )
    p.add_argument(
        "--cookies-browser",
        metavar="{chrome,firefox,edge,opera,brave}",
        help="Czytaj ciasteczka z przeglądarki (wymagane dla treści 18+ na CDA itp.).",
    )

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

    progress.warn(f"Nie rozpoznano formatu zakresu odcinków: {range_str!r} — ignoruję.")
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
            with progress.spinner(f"Czekam na API shinden ({ANTIBOT_LABEL}) …"):
                embed = shinden_api.resolve_embed(client, candidate.online_id)
            _url_short = embed.url if len(embed.url) <= 80 else embed.url[:77] + "…"
            progress.info(f"Embed: {_url_short}")
            stream = extract.resolve(
                embed.url,
                embed.referer,
                cookies_file=cookies_file,
                cookies_browser=cookies_browser,
            )
            return stream, embed
        except (NoStreamError, AntiBotError) as exc:
            progress.warn(f"Player {candidate.player!r} (ep {ep_number:g}) nie zadziałał: {exc}")

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


def main() -> None:  # noqa: C901
    _setup_encoding()
    parser = _build_parser()
    args = parser.parse_args()

    # --- delete history -------------------------------------------------------
    if args.delete_history:
        history.clear()
        progress.success("Historia wyczyszczona.")
        sys.exit(0)

    client = shinden_http.make_client()
    player_kind = "vlc" if args.vlc else "mpv"
    interactive = sys.stdin.isatty() and not args.select_nth

    try:
        # --- resolve series ---------------------------------------------------
        if not args.query and not args.url and not args.resume:
            if not interactive:
                parser.error("Podaj tytuł do wyszukania, URL (--url) lub użyj -c.")
            all_entries = history.list_all()
            mode = menus.select_start_mode(
                has_history=bool(all_entries),
                history_count=len(all_entries),
            )
            if mode == "quit":
                sys.exit(0)
            elif mode == "resume":
                args.resume = True
            elif mode == "url":
                args.url = menus.prompt_url()
            else:
                args.query = [menus.prompt_search_query()]

        if args.resume:
            all_entries = history.list_all()
            if not all_entries:
                progress.error("Historia jest pusta.")
                sys.exit(1)
            ref, last_ep = menus.select_series_from_history(all_entries)
        elif args.url:
            ref = shinden_series.parse_series_url(args.url)
            last_ep = 0.0
        else:
            query = " ".join(args.query)
            progress.info(f"Szukam: {query!r}")
            hits = shinden_search.search_series(client, query)
            if not hits:
                progress.error(f"Brak wyników dla: {query!r}")
                sys.exit(1)

            if args.select_nth or not sys.stdin.isatty():
                idx = (args.select_nth - 1) if args.select_nth else 0
                if idx < 0 or idx >= len(hits):
                    progress.error(f"--select-nth {args.select_nth}: jest tylko {len(hits)} wyników.")
                    sys.exit(1)
                ref = shinden_series.parse_series_url(hits[idx].url)
                ref = SeriesRef(id=ref.id, slug=ref.slug, title=hits[idx].title, url=ref.url)
            else:
                hit = menus.select_series(hits)
                ref = shinden_series.parse_series_url(hit.url)
                ref = SeriesRef(id=ref.id, slug=ref.slug, title=hit.title, url=ref.url)

            last_ep = 0.0

        # --- episode list -----------------------------------------------------
        progress.info(f"Pobieram listę odcinków: {ref.title}")
        ref, episodes = shinden_series.list_episodes(client, ref)

        if not episodes:
            progress.error("Brak dostępnych odcinków.")
            sys.exit(1)

        # --- resolve target episodes -----------------------------------------
        if args.episode:
            targets = _parse_range(args.episode, episodes)
            if not targets:
                progress.error(f"Nie znaleziono odcinków dla zakresu: {args.episode!r}")
                sys.exit(1)
        elif args.resume and last_ep > 0:
            # Start from the first unwatched episode
            remaining = [ep for ep in episodes if ep.number > last_ep]
            if not remaining:
                progress.warn(f"Obejrzałeś już wszystkie odcinki ({ref.title}).")
                remaining = episodes
            targets = menus.select_episodes(remaining, prompt=f"Wybierz odcinek ({ref.title})", multi=True)
        elif not sys.stdin.isatty():
            targets = [episodes[0]]
        else:
            targets = menus.select_episodes(episodes, prompt=f"Wybierz odcinek ({ref.title})", multi=True)

        if not targets:
            progress.warn("Nie wybrano żadnych odcinków.")
            sys.exit(0)

        # --- play each episode -----------------------------------------------
        _episode_action: str | None = None  # set once on first episode, reused for the rest
        for ep in targets:
            progress.info(f"Odcinek {ep.number:g}: {ep.title}")

            ep_resp = client.get(ep.url)
            ep_resp.raise_for_status()
            players = shinden_episode.sort_players(
                shinden_episode.parse_players(ep_resp.text),
                download=args.download,
            )

            if not players:
                progress.warn(f"Brak playerów dla odcinka {ep.number:g} — pomijam.")
                continue

            players = _filter_players(
                players,
                lang=args.lang,
                subs=args.subs,
                player_name=args.player_name,
            )

            _auto = args.select_nth or len(players) == 1 or not sys.stdin.isatty()
            _failed_ids: set[str] = set()
            stream, embed, chosen = None, None, players[0]

            while True:
                if _auto:
                    chosen = players[0]
                else:
                    if _failed_ids:
                        progress.warn(f"Player {chosen.player!r} nie zadziałał — wybierz inny:")
                    chosen = menus.select_player(
                        players,
                        prompt=f"Player — ep {ep.number:g}",
                        failed=_failed_ids,
                    )

                stream, embed = _resolve_with_fallback(
                    client,
                    players,
                    chosen,
                    _auto,
                    ep.number,
                    cookies_file=args.cookies_file,
                    cookies_browser=args.cookies_browser,
                )

                if stream is not None or _auto:
                    break
                _failed_ids.add(chosen.online_id)
                if all(p.online_id in _failed_ids for p in players):
                    break

            if stream is None:
                progress.warn(f"Żaden player nie zadziałał dla odcinka {ep.number:g} — pomijam.")
                continue

            if args.quality is None:
                if interactive and stream.qualities:
                    args.quality = menus.select_quality(stream.qualities)
                else:
                    args.quality = "best"
            stream = _pick_quality(stream, args.quality)
            title = f"{ref.title} — Odcinek {ep.number:g}"

            if args.download:
                _action = "download"
            elif args.debug:
                _action = "debug"
            elif interactive and _episode_action is None:
                _episode_action = menus.select_action()
                _action = _episode_action
            else:
                _action = _episode_action or "play"

            if _action == "download":
                download.run(stream, ep, ref)
            elif _action == "debug":
                _print_debug(stream, embed)
            else:
                player_runner.play(
                    stream,
                    kind=player_kind,
                    title=title,
                    no_detach=args.no_detach,
                )
                progress.success(f"Odtwarzam w {player_kind}: {title}")

            history.upsert(ref, last_ep=ep.number)

    except KeyboardInterrupt:
        progress.warn("Przerwano przez użytkownika.")
        sys.exit(130)
    except (AntiBotError, NoStreamError, ParseError) as exc:
        progress.error(str(exc))
        sys.exit(1)
    except PlayerNotFoundError as exc:
        progress.error(str(exc))
        sys.exit(1)
    except ShindenError as exc:
        progress.error(f"Błąd shinden: {exc}")
        sys.exit(1)
    finally:
        client.close()


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


ANTIBOT_LABEL = "5 s antibot delay"
