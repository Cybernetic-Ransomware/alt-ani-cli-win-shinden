"""Interactive menus — InquirerPy with fallback to numbered prompt.

InquirerPy (prompt_toolkit) fails in git-bash with TERM=xterm-256color on
Windows because it tries to use the Win32 console API via xterm emulation.
In that environment we fall back to a simple numbered list + input().

All functions return None when the user presses ESC (InquirerPy) or an empty
Enter (numbered fallback), signalling "go back".
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from alt_ani_cli.content import CONTENT
from alt_ani_cli.shinden.models import EpisodeRow, PlayerEntry, SeriesHit, SeriesRef

_RES_RE = re.compile(r"(\d+)")
_M = CONTENT["menu"]
_FB = _M["fallback"]


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


# Bind ESC to the "interrupt" action so it returns None without requiring
# mandatory=False.  "skip" needs mandatory=False which adds an extra Enter
# handler that races with _handle_enter and causes "Return value already set"
# on CPython 3.14 + Windows.  "interrupt" with raise_keyboard_interrupt=False
# exits cleanly with None and has no such side-effect.
# _keybinding_factory() runs inside __init__, so this must be set at
# construction time — mutating kb_maps after the fact has no effect.
_BACK_KB: dict = {"interrupt": [{"key": "escape"}]}


def _ask(prompt_obj):
    """Execute an InquirerPy prompt; return None on ESC or Ctrl-C."""
    try:
        return prompt_obj.execute()
    except KeyboardInterrupt:
        return None


def _numbered_pick(items: list, label_fn, prompt: str):
    """Single-pick numbered menu; returns None when user presses empty Enter."""
    for i, item in enumerate(items, 1):
        print(f"  {i}. {label_fn(item)}")
    while True:
        try:
            raw = input(_FB["select_prompt"].format(prompt=prompt, n=len(items))).strip()
            if not raw:
                return None
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError, KeyboardInterrupt:
            pass
        print(_FB["invalid_number"].format(n=len(items)))


def _numbered_pick_multi(items: list, label_fn, prompt: str) -> list | None:
    """Multi-pick numbered menu; returns None when user presses empty Enter."""
    for i, item in enumerate(items, 1):
        print(f"  {i}. {label_fn(item)}")
    print(_FB["multi_hint"])
    while True:
        try:
            raw = input(_FB["select_prompt"].format(prompt=prompt, n=len(items))).strip()
            if not raw:
                return None
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
        print(_FB["invalid_numbers"].format(n=len(items)))


def select_series(
    hits: list[SeriesHit],
    prompt: str = _M["series"]["default_prompt"],
) -> SeriesHit | None:
    def _label(h: SeriesHit) -> str:
        t = h.series_type or "?"
        return f"{h.title}  [{t}]  (id:{h.id})"

    if not _use_inquirer():
        return _numbered_pick(hits, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(h)) for i, h in enumerate(hits)]
    idx = _ask(
        inquirer.fuzzy(
            message=f"{prompt}:",
            choices=choices,
            max_height="40%",
            long_instruction=_M["series"]["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return None if idx is None else hits[idx]


def select_series_from_history(
    entries: list[tuple[SeriesRef, float]],
    prompt: str = _M["history_resume"]["default_prompt"],
) -> tuple[SeriesRef, float] | None:
    def _label(e: tuple[SeriesRef, float]) -> str:
        return _M["history_resume"]["label"].format(title=e[0].title, last_ep=e[1])

    if not _use_inquirer():
        return _numbered_pick(entries, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(e)) for i, e in enumerate(entries)]
    idx = _ask(
        inquirer.fuzzy(
            message=f"{prompt}:",
            choices=choices,
            max_height="40%",
            long_instruction=_M["history_resume"]["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return None if idx is None else entries[idx]


def select_episodes(
    episodes: list[EpisodeRow],
    prompt: str = _M["episodes"]["default_prompt"],
    multi: bool = False,
    watched_numbers: set[float] | None = None,
) -> list[EpisodeRow] | None:
    _watched = watched_numbers or set()
    _ep = _M["episodes"]

    def _label(ep: EpisodeRow) -> str:
        tmpl = _ep["label_watched"] if ep.number in _watched else _ep["label_unwatched"]
        return tmpl.format(number=ep.number, title=ep.title)

    if not _use_inquirer():
        if multi:
            result = _numbered_pick_multi(episodes, _label, prompt)
            return result  # None or list
        picked = _numbered_pick(episodes, _label, prompt)
        return None if picked is None else [picked]

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(ep)) for i, ep in enumerate(episodes)]

    if multi:
        _name_map = {i: _label(ep) for i, ep in enumerate(episodes)}
        indices = _ask(
            inquirer.checkbox(
                message=f"{prompt}:",
                choices=choices,
                validate=lambda result: len(result) > 0,
                invalid_message=_ep["invalid_multi"],
                long_instruction=_ep["instruction_multi"],
                transformer=lambda result: ", ".join(_name_map.get(r, str(r)) for r in result),
                mandatory=False,
                raise_keyboard_interrupt=False,
                keybindings=_BACK_KB,
            )
        )
        return None if indices is None else [episodes[i] for i in indices]

    idx = _ask(
        inquirer.fuzzy(
            message=f"{prompt}:",
            choices=choices,
            max_height="60%",
            long_instruction=_ep["instruction_single"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return None if idx is None else [episodes[idx]]


def select_player(
    players: list[PlayerEntry],
    prompt: str = _M["player"]["default_prompt"],
    failed: set[str] | None = None,
) -> PlayerEntry | None:
    _failed = failed or set()
    _pl = _M["player"]

    def _label(p: PlayerEntry) -> str:
        audio = _lang_tag(p.lang_audio)
        subs = f"+{_lang_tag(p.lang_subs)}" if p.lang_subs else ""
        res = f" [{p.max_res}]" if p.max_res else ""
        tmpl = _pl["label_failed"] if p.online_id in _failed else _pl["label_ok"]
        return tmpl.format(player=p.player, res=res, audio=audio, subs=subs)

    if not _use_inquirer():
        return _numbered_pick(players, _label, prompt)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(p)) for i, p in enumerate(players)]
    idx = _ask(
        inquirer.select(
            message=f"{prompt}:",
            choices=choices,
            long_instruction=_pl["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return None if idx is None else players[idx]


def select_start_mode(has_history: bool, history_count: int = 0) -> Literal["search", "resume", "url", "quit"] | None:
    _sm = _M["start_mode"]
    _opts = _sm["options"]

    options_plain = []
    options_plain.append(("search", f"1. {_opts['search']}"))
    if has_history:
        resume_label = _opts["resume_with_count"].format(count=history_count) if history_count else _opts["resume"]
        options_plain.append(("resume", f"2. {resume_label}"))
    options_plain.append(("url", f"{len(options_plain) + 1}. {_opts['url']}"))
    options_plain.append(("quit", f"{len(options_plain) + 1}. {_opts['quit']}"))

    if not _use_inquirer():
        for _, label in options_plain:
            print(f"  {label}")
        keys = [k for k, _ in options_plain]
        while True:
            try:
                raw = input(_sm["fallback_prompt"].format(n=len(keys))).strip()
                if not raw:
                    return None
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]  # type: ignore[return-value]
            except ValueError, KeyboardInterrupt:
                pass
            print(_sm["fallback_invalid"].format(n=len(keys)))

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=key, name=label.split(". ", 1)[1]) for key, label in options_plain]
    return _ask(
        inquirer.select(
            message=_sm["question"],
            choices=choices,
            long_instruction=_sm["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )


def prompt_search_query() -> str | None:
    _sq = _M["search_query"]

    if not _use_inquirer():
        while True:
            raw = input(_sq["fallback_prompt"]).strip()
            if not raw:
                return None
            return raw

    from InquirerPy import inquirer

    result = _ask(
        inquirer.text(
            message=_sq["message"],
            validate=lambda s: bool(s.strip()),
            invalid_message=_sq["invalid"],
            long_instruction=_sq["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return result.strip() if result is not None else None


def prompt_url() -> str | None:
    _u = _M["url"]

    def _valid(s: str) -> bool:
        try:
            return urlparse(s.strip()).netloc.endswith("shinden.pl")
        except Exception:
            return False

    if not _use_inquirer():
        while True:
            raw = input(_u["fallback_prompt"]).strip()
            if not raw:
                return None
            if _valid(raw):
                return raw
            print(_u["fallback_invalid"])

    from InquirerPy import inquirer

    result = _ask(
        inquirer.text(
            message=_u["message"],
            validate=_valid,
            invalid_message=_u["invalid"],
            long_instruction=_u["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return result.strip() if result is not None else None


def select_quality(qualities: dict[str, str], prompt: str = _M["quality"]["default_prompt"]) -> str | None:
    if not qualities:
        return "best"

    _q = _M["quality"]

    def _height(key: str) -> float:
        m = _RES_RE.search(key)
        return float(m.group(1)) if m else 0.0

    sorted_keys = sorted(qualities.keys(), key=_height, reverse=True)
    all_options = ["best"] + sorted_keys + ["worst"]

    def _label(opt: str) -> str:
        if opt == "best":
            return _q["label_best"].format(top=sorted_keys[0])
        if opt == "worst":
            return _q["label_worst"].format(bottom=sorted_keys[-1])
        return opt

    if not _use_inquirer():
        for i, opt in enumerate(all_options, 1):
            print(f"  {i}. {_label(opt)}")
        while True:
            try:
                raw = input(_FB["select_prompt"].format(prompt=prompt, n=len(all_options))).strip()
                if not raw:
                    return None
                idx = int(raw) - 1
                if 0 <= idx < len(all_options):
                    return all_options[idx]
            except ValueError, KeyboardInterrupt:
                pass
            print(_FB["invalid_number"].format(n=len(all_options)))

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=opt, name=_label(opt)) for opt in all_options]
    return _ask(
        inquirer.select(
            message=f"{prompt}:",
            choices=choices,
            long_instruction=_q["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )


def select_action() -> Literal["play", "download", "debug"] | None:
    _ac = _M["action"]
    _ac_opts = _ac["options"]
    _options: list[tuple[str, str]] = [
        ("play", _ac_opts["play"]),
        ("download", _ac_opts["download"]),
        ("debug", _ac_opts["debug"]),
    ]

    if not _use_inquirer():
        for i, (_, label) in enumerate(_options, 1):
            print(f"  {i}. {label}")
        keys = [k for k, _ in _options]
        while True:
            try:
                raw = input(_ac["fallback_prompt"]).strip()
                if not raw:
                    return None
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]  # type: ignore
            except ValueError, KeyboardInterrupt:
                pass
            print(_ac["fallback_invalid"])

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=key, name=label) for key, label in _options]
    return _ask(
        inquirer.select(
            message=_ac["message"],
            choices=choices,
            long_instruction=_ac["instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
