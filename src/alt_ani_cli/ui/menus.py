"""Interactive menus — InquirerPy with fallback to numbered prompt.

InquirerPy (prompt_toolkit) fails in git-bash with TERM=xterm-256color on
Windows because it tries to use the Win32 console API via xterm emulation.
In that environment we fall back to a simple numbered list + input().

All functions return None when the user presses ESC (InquirerPy) or an empty
Enter (numbered fallback), signalling "go back".
"""

import re
from contextlib import suppress
from typing import Literal
from urllib.parse import urlparse

from alt_ani_cli.content import CONTENT
from alt_ani_cli.models import EpisodeRow, PlayerEntry, RelatedSeries, SeriesHit, SeriesMetadata, SeriesRef
from alt_ani_cli.ui import progress

_RES_RE = re.compile(r"(\d+)")
_M = CONTENT["menu"]
_FB = _M["fallback"]
_EMPTY_META = SeriesMetadata(None, None, "", (), ())


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


# kb_maps / kb_func_lookup setters MERGE with BaseSimplePrompt._kb_maps, so
# the base "skip" binding (c-c when raise_keyboard_interrupt=False) is always
# active unless we explicitly clear it.  With mandatory=True, _handle_skip
# never exits the prompt — it only renders "Mandatory prompt" in the footer,
# making Ctrl+C a visible no-op.
#
# Fix: set "skip": [] to remove all skip bindings, and route both ESC and
# Ctrl+C through "interrupt" instead.  _handle_interrupt always calls
# event.app.exit(INQUIRERPY_KEYBOARD_INTERRUPT), which execute() converts to
# a KeyboardInterrupt; _ask() catches that and returns None (= go back).
# No "Mandatory prompt" flash, no double-exit crash on CPython 3.14 + Windows.
#
# _keybinding_factory() runs inside __init__, so keybindings= must be set at
# construction time — mutating kb_maps after the fact has no effect.
_BACK_KB: dict = {"interrupt": [{"key": "escape"}, {"key": "c-c"}], "skip": []}


def _ask(prompt_obj):
    """Execute an InquirerPy prompt; return None on ESC or Ctrl-C."""
    try:
        return prompt_obj.execute()
    except KeyboardInterrupt:
        return None


def _run_simple_picker(
    items: list,
    label_fn,
    *,
    prompt: str,
    instruction: str,
    mode: Literal["fuzzy", "select"] = "fuzzy",
    max_height: str = "40%",
):
    """One-shot picker: numbered fallback + InquirerPy fuzzy or select.

    Returns the selected item, or None on ESC / empty Enter.
    """
    if not _use_inquirer():
        return _numbered_pick(items, label_fn, prompt)
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=label_fn(it)) for i, it in enumerate(items)]
    if mode == "fuzzy":
        idx = _ask(
            inquirer.fuzzy(
                message=f"{prompt}:",
                choices=choices,
                max_height=max_height,
                long_instruction=instruction,
                raise_keyboard_interrupt=False,
                keybindings=_BACK_KB,
            )
        )
    else:
        idx = _ask(
            inquirer.select(
                message=f"{prompt}:",
                choices=choices,
                long_instruction=instruction,
                raise_keyboard_interrupt=False,
                keybindings=_BACK_KB,
            )
        )
    return None if idx is None else items[idx]


def _run_keyed_picker(
    options: list[tuple[str, str]],
    *,
    prompt: str,
    instruction: str,
    fallback_invalid: str,
):
    """One-shot key-value picker where values are string keys (not list indices).

    options: [(key, display_label), ...]
    Returns the selected key string, or None on ESC / empty Enter.
    """
    if not _use_inquirer():
        for i, (_, label) in enumerate(options, 1):
            print(f"  {i}. {label}")
        keys = [k for k, _ in options]
        while True:
            try:
                raw = input(_FB["select_prompt"].format(prompt=prompt, n=len(keys))).strip()
                if not raw:
                    return None
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]
            except ValueError, KeyboardInterrupt:
                pass
            print(fallback_invalid)
    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=key, name=label) for key, label in options]
    return _ask(
        inquirer.select(
            message=f"{prompt}:",
            choices=choices,
            long_instruction=instruction,
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )


def confirm(message: str) -> bool | None:
    """Yes/No prompt. Returns True/False, or None on ESC / empty Enter."""
    _c = _M["confirm"]
    key = _run_keyed_picker(
        [("yes", _c["option_yes"]), ("no", _c["option_no"])],
        prompt=message,
        instruction=_c["instruction"],
        fallback_invalid=_c["fallback_invalid"],
    )
    return None if key is None else key == "yes"


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


def _series_label(h: SeriesHit, meta: SeriesMetadata | None) -> str:
    t = h.series_type or "?"
    _s = _M["series"]
    if meta and meta.air_date:
        return _s["label_with_date"].format(title=h.title, type=t, date=meta.air_date, id=h.id)
    return _s["label_without_date"].format(title=h.title, type=t, id=h.id)


def _register_series_kbs(prompt_obj, signal: dict) -> None:
    def _idx() -> int:
        try:
            return prompt_obj.content_control.selection["value"]
        except Exception:
            return 0

    # eager=True is required: the fuzzy prompt's BufferControl has emacs bindings
    # (e.g. c-r = reverse-i-search) at control level, which beat _kb-level bindings
    # when KeyProcessor picks matches[-1].  eager overrides the control-level handler.
    @prompt_obj._kb.add("c-s", eager=True)
    def _(event):
        signal["sig"] = ("sort", _idx())
        event.app.exit(result=None)

    @prompt_obj._kb.add("c-o", eager=True)
    def _(event):
        signal["sig"] = ("desc", _idx())
        event.app.exit(result=None)

    @prompt_obj._kb.add("c-q", eager=True)
    def _(event):
        signal["sig"] = ("tags", _idx())
        event.app.exit(result=None)

    @prompt_obj._kb.add("c-r", eager=True)
    def _(event):
        signal["sig"] = ("related", _idx())
        event.app.exit(result=None)


def show_modal_text(title: str, body: str) -> None:
    """Render a blocking modal panel (description or tags) and wait for Enter."""
    progress.rule(title)
    progress.output(body)
    progress.output(f"\n[dim]{_M['series']['press_any_key']}[/dim]")
    with suppress(EOFError, KeyboardInterrupt):
        input()


def pick_related(items: tuple[RelatedSeries, ...]) -> RelatedSeries | None:
    """Prompt the user to pick a related series (or return None if empty / ESC)."""
    _s = _M["series"]
    if not items:
        progress.warn(_s["related_empty"])
        with suppress(EOFError, KeyboardInterrupt):
            input(_s["press_any_key"] + " ")
        return None

    def _label(r: RelatedSeries) -> str:
        return _s["related_label"].format(title=r.title, relation=r.relation)

    if not _use_inquirer():
        return _numbered_pick(list(items), _label, _s["related_pick_prompt"])

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_label(r)) for i, r in enumerate(items)]
    idx = _ask(
        inquirer.select(
            message=f"{_s['related_pick_prompt']}:",
            choices=choices,
            long_instruction=_s["related_instruction"],
            raise_keyboard_interrupt=False,
            keybindings=_BACK_KB,
        )
    )
    return None if idx is None else items[idx]


def select_series_once(
    hits: list[SeriesHit],
    metadata: dict[str, SeriesMetadata] | None = None,
    *,
    prompt: str = _M["series"]["default_prompt"],
) -> tuple:
    """Render the series picker once and return a signal tuple.

    Return values:
      ("pick",    SeriesHit)   — user confirmed a selection
      ("back",    None)        — ESC or Ctrl-C
      ("sort",    cursor_idx)  — Ctrl+S: caller should toggle sort order
      ("desc",    cursor_idx)  — Ctrl+O: caller should show description modal
      ("tags",    cursor_idx)  — Ctrl+Q: caller should show tags modal
      ("related", cursor_idx)  — Ctrl+R: caller should show related picker

    cursor_idx is an index into the *hits* list that was passed in.
    The fallback numbered menu only produces ("pick", ...) or ("back", None).
    """
    _meta: dict[str, SeriesMetadata] = dict(metadata or {})

    if not _use_inquirer():
        hit = _numbered_pick(hits, lambda h: _series_label(h, _meta.get(h.id)), prompt)
        return ("back", None) if hit is None else ("pick", hit)

    from InquirerPy import inquirer
    from InquirerPy.base.control import Choice

    choices = [Choice(value=i, name=_series_label(h, _meta.get(h.id))) for i, h in enumerate(hits)]
    signal: dict = {"sig": None}
    prompt_obj = inquirer.fuzzy(
        message=f"{prompt}:",
        choices=choices,
        max_height="40%",
        long_instruction=_M["series"]["instruction"],
        raise_keyboard_interrupt=False,
        keybindings=_BACK_KB,
    )
    _register_series_kbs(prompt_obj, signal)
    try:
        result = prompt_obj.execute()
    except KeyboardInterrupt:
        return ("back", None)

    if signal["sig"] is not None:
        action, idx = signal["sig"]
        return (action, idx if idx is not None else 0)

    return ("back", None) if result is None else ("pick", hits[result])


def select_series_from_history(
    entries: list[tuple[SeriesRef, float]],
    prompt: str = _M["history_resume"]["default_prompt"],
) -> tuple[SeriesRef, float] | None:
    _hr = _M["history_resume"]
    return _run_simple_picker(
        entries,
        lambda e: _hr["label"].format(title=e[0].title, last_ep=e[1]),
        prompt=prompt,
        instruction=_hr["instruction"],
    )


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
            return _numbered_pick_multi(episodes, _label, prompt)
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

    ep = _run_simple_picker(
        episodes,
        _label,
        prompt=prompt,
        instruction=_ep["instruction_single"],
        max_height="60%",
    )
    return None if ep is None else [ep]


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

    return _run_simple_picker(players, _label, prompt=prompt, instruction=_pl["instruction"], mode="select")


def select_start_mode(has_history: bool, history_count: int = 0) -> Literal["search", "resume", "url", "quit"] | None:
    _sm = _M["start_mode"]
    _opts = _sm["options"]

    options_plain: list[tuple[str, str]] = [("search", _opts["search"])]
    if has_history:
        resume_label = _opts["resume_with_count"].format(count=history_count) if history_count else _opts["resume"]
        options_plain.append(("resume", resume_label))
    options_plain.append(("url", _opts["url"]))
    options_plain.append(("quit", _opts["quit"]))

    return _run_keyed_picker(  # type: ignore[return-value]
        options_plain,
        prompt=_sm["question"],
        instruction=_sm["instruction"],
        fallback_invalid=_sm["fallback_invalid"].format(n=len(options_plain)),
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

    return _run_simple_picker(all_options, _label, prompt=prompt, instruction=_q["instruction"], mode="select")


def select_action() -> Literal["play", "download", "debug"] | None:
    _ac = _M["action"]
    _ac_opts = _ac["options"]
    _options: list[tuple[str, str]] = [
        ("play", _ac_opts["play"]),
        ("download", _ac_opts["download"]),
        ("debug", _ac_opts["debug"]),
    ]
    return _run_keyed_picker(  # type: ignore[return-value]
        _options,
        prompt=_ac["message"],
        instruction=_ac["instruction"],
        fallback_invalid=_ac["fallback_invalid"],
    )
