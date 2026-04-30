"""Interactive menus — InquirerPy with fallback to numbered prompt.

InquirerPy (prompt_toolkit) fails in git-bash with TERM=xterm-256color on
Windows because it tries to use the Win32 console API via xterm emulation.
In that environment we fall back to a simple numbered list + input().
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesRef

_RES_RE = re.compile(r"(\d+)")


def _lang_tag(lang: str) -> str:
    return lang.upper() if lang else ""


def _can_use_inquirer() -> bool:
    """Return True only when prompt_toolkit can open a real console output."""
    import sys

    if not sys.stdout.isatty():
        return False
    # git-bash sets TERM=xterm-256color but the Win32 console API is not
    # available → prompt_toolkit raises NoConsoleScreenBufferError.
    # Detect by trying to import and create a dummy output.
    try:
        from prompt_toolkit.output.defaults import create_output

        create_output()
        return True
    except Exception:
        return False


_USE_INQUIRER: bool | None = None


def _use_inquirer() -> bool:
    global _USE_INQUIRER
    if _USE_INQUIRER is None:
        _USE_INQUIRER = _can_use_inquirer()
    return _USE_INQUIRER


def _numbered_pick(items: list, label_fn, prompt: str):
    for i, item in enumerate(items, 1):
        print(f"  {i}. {label_fn(item)}")
    while True:
        try:
            raw = input(f"{prompt} [1-{len(items)}]: ").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError, KeyboardInterrupt:
            pass
        print(f"  Wpisz liczbę 1–{len(items)}.")


def _numbered_pick_multi(items: list, label_fn, prompt: str) -> list:
    for i, item in enumerate(items, 1):
        print(f"  {i}. {label_fn(item)}")
    print('  (wpisz numery oddzielone spacją, np. "1 3 5", lub zakres "2-4")')
    while True:
        try:
            raw = input(f"{prompt} [1-{len(items)}]: ").strip()
            if not raw:
                continue
            indices: set[int] = set()
            for token in raw.split():
                if "-" in token:
                    a, _, b = token.partition("-")
                    lo, hi = int(a) - 1, int(b) - 1
                    indices.update(range(lo, hi + 1))
                else:
                    indices.add(int(token) - 1)
            valid = sorted(i for i in indices if 0 <= i < len(items))
            if valid:
                return [items[i] for i in valid]
        except ValueError, KeyboardInterrupt:
            pass
        print(f"  Wpisz poprawne numery 1–{len(items)}.")


def select_series(hits: list[SeriesHit], prompt: str = "Wybierz serię") -> SeriesHit:
    def _label(h: SeriesHit) -> str:
        t = h.series_type or "?"
        return f"{h.title}  [{t}]  (id:{h.id})"

    if not _use_inquirer():
        return _numbered_pick(hits, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(h)) for i, h in enumerate(hits)]
    idx = inquirer.fuzzy(
        message=f"{prompt}:",
        choices=choices,
        max_height="40%",
        long_instruction="Wpisz aby filtrować  |  Enter = wybierz",
    ).execute()
    return hits[idx]


def select_series_from_history(
    entries: list[tuple[SeriesRef, float]],
    prompt: str = "Kontynuuj oglądanie",
) -> tuple[SeriesRef, float]:
    def _label(e: tuple[SeriesRef, float]) -> str:
        return f"{e[0].title}  — ostatni ep {e[1]:g}"

    if not _use_inquirer():
        return _numbered_pick(entries, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(e)) for i, e in enumerate(entries)]
    idx = inquirer.fuzzy(
        message=f"{prompt}:",
        choices=choices,
        max_height="40%",
        long_instruction="Wpisz aby filtrować  |  Enter = wybierz",
    ).execute()
    return entries[idx]


def select_episodes(
    episodes: list[EpisodeRow],
    prompt: str = "Wybierz odcinek",
    multi: bool = False,
) -> list[EpisodeRow]:
    def _label(ep: EpisodeRow) -> str:
        return f"{ep.number:g}.  {ep.title}"

    if not _use_inquirer():
        if multi:
            return _numbered_pick_multi(episodes, _label, prompt)
        return [_numbered_pick(episodes, _label, prompt)]

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(ep)) for i, ep in enumerate(episodes)]

    if multi:
        _name_map = {i: _label(ep) for i, ep in enumerate(episodes)}
        indices = inquirer.checkbox(
            message=f"{prompt}:",
            choices=choices,
            validate=lambda result: len(result) > 0,
            invalid_message="Zaznacz co najmniej jeden odcinek — użyj Spacji.",
            long_instruction=("Wpisz aby filtrować  |  Spacja = zaznacz/odznacz  |  Enter = potwierdź"),
            transformer=lambda result: ", ".join(_name_map.get(r, str(r)) for r in result),
        ).execute()
        return [episodes[i] for i in indices]

    idx = inquirer.fuzzy(
        message=f"{prompt}:",
        choices=choices,
        max_height="60%",
        long_instruction="Wpisz aby filtrować  |  Enter = wybierz",
    ).execute()
    return [episodes[idx]]


def select_player(
    players: list[PlayerEntry],
    prompt: str = "Wybierz player",
    failed: set[str] | None = None,
) -> PlayerEntry:
    _failed = failed or set()

    def _label(p: PlayerEntry) -> str:
        audio = _lang_tag(p.lang_audio)
        subs = f"+{_lang_tag(p.lang_subs)}" if p.lang_subs else ""
        res = f" [{p.max_res}]" if p.max_res else ""
        mark = "✗ " if p.online_id in _failed else "  "
        return f"{mark}{p.player}{res}  {audio}{subs}"

    if not _use_inquirer():
        return _numbered_pick(players, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(p)) for i, p in enumerate(players)]
    idx = inquirer.select(
        message=f"{prompt}:",
        choices=choices,
        long_instruction="↑↓ = nawigacja  |  Enter = wybierz",
    ).execute()
    return players[idx]


def select_start_mode(has_history: bool, history_count: int = 0) -> Literal["search", "resume", "url", "quit"]:
    options_plain = []
    options_plain.append(("search", "1. Szukaj nowego anime"))
    if has_history:
        n = f" ({history_count} pozycji)" if history_count else ""
        options_plain.append(("resume", f"2. Kontynuuj z historii{n}"))
    options_plain.append(("url", f"{len(options_plain) + 1}. Wklej URL serii"))
    options_plain.append(("quit", f"{len(options_plain) + 1}. Wyjdź"))

    if not _use_inquirer():
        for _, label in options_plain:
            print(f"  {label}")
        keys = [k for k, _ in options_plain]
        while True:
            try:
                raw = input(f"Wybór [1-{len(keys)}]: ").strip()
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]  # type: ignore[return-value]
            except ValueError, KeyboardInterrupt:
                pass
            print(f"  Wpisz liczbę 1–{len(keys)}.")

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=key, name=label.split(". ", 1)[1]) for key, label in options_plain]
    return inquirer.select(
        message="Co chcesz zrobić?",
        choices=choices,
        long_instruction="↑↓ = nawigacja  |  Enter = wybierz",
    ).execute()


def prompt_search_query() -> str:
    if not _use_inquirer():
        while True:
            raw = input("Czego szukasz: ").strip()
            if raw:
                return raw

    from InquirerPy import inquirer

    return (
        inquirer.text(
            message="Czego szukasz?",
            validate=lambda s: bool(s.strip()),
            invalid_message="Wpisz tytuł anime.",
        )
        .execute()
        .strip()
    )


def prompt_url() -> str:
    def _valid(s: str) -> bool:
        try:
            return urlparse(s.strip()).netloc.endswith("shinden.pl")
        except Exception:
            return False

    if not _use_inquirer():
        while True:
            raw = input("URL serii (https://shinden.pl/series/...): ").strip()
            if _valid(raw):
                return raw
            print("  Podaj prawidłowy URL z shinden.pl.")

    from InquirerPy import inquirer

    return (
        inquirer.text(
            message="URL serii (https://shinden.pl/series/...):",
            validate=_valid,
            invalid_message="Podaj prawidłowy URL z shinden.pl.",
        )
        .execute()
        .strip()
    )


def select_quality(qualities: dict[str, str], prompt: str = "Wybierz jakość") -> str:
    if not qualities:
        return "best"

    def _height(key: str) -> float:
        m = _RES_RE.search(key)
        return float(m.group(1)) if m else 0.0

    sorted_keys = sorted(qualities.keys(), key=_height, reverse=True)
    all_options = ["best"] + sorted_keys + ["worst"]

    def _label(opt: str) -> str:
        if opt == "best":
            return f"best  (najwyższa dostępna — {sorted_keys[0]})"
        if opt == "worst":
            return f"worst  (najniższa dostępna — {sorted_keys[-1]})"
        return opt

    if not _use_inquirer():
        for i, opt in enumerate(all_options, 1):
            print(f"  {i}. {_label(opt)}")
        while True:
            try:
                raw = input(f"{prompt} [1-{len(all_options)}]: ").strip()
                idx = int(raw) - 1
                if 0 <= idx < len(all_options):
                    return all_options[idx]
            except ValueError, KeyboardInterrupt:
                pass
            print(f"  Wpisz liczbę 1–{len(all_options)}.")

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=opt, name=_label(opt)) for opt in all_options]
    return inquirer.select(
        message=f"{prompt}:",
        choices=choices,
        long_instruction="↑↓ = nawigacja  |  Enter = wybierz",
    ).execute()


def select_action() -> Literal["play", "download", "debug"]:
    _options: list[tuple[str, str]] = [
        ("play", "Oglądaj w mpv/vlc"),
        ("download", "Pobierz na dysk"),
        ("debug", "Pokaż linki (debug)"),
    ]

    if not _use_inquirer():
        for i, (_, label) in enumerate(_options, 1):
            print(f"  {i}. {label}")
        keys = [k for k, _ in _options]
        while True:
            try:
                raw = input("Akcja [1-3]: ").strip()
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]  # type: ignore
            except ValueError, KeyboardInterrupt:
                pass
            print("  Wpisz liczbę 1–3.")

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=key, name=label) for key, label in _options]
    return inquirer.select(
        message="Co zrobić z tym odcinkiem?",
        choices=choices,
        long_instruction="↑↓ = nawigacja  |  Enter = wybierz",
    ).execute()
