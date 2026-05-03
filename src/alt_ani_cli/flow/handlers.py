"""FSM handlers — one function per Screen, called by the main loop in cli.py.

Each handler receives FlowState, mutates it as needed, and returns the next
Screen, BACK (go one step back in history), or None (exit the application).

Handlers for virtual screens (FETCH_EPISODES, EPISODE_DISPATCH, RESOLVE_STREAM,
RUN_ACTION) contain no UI; they perform I/O or computation and always return a
concrete next Screen (never BACK — the main loop never pushes them onto the
history stack).
"""

from __future__ import annotations

from collections.abc import Callable

from alt_ani_cli import download, history
from alt_ani_cli.flow.state import BACK, FlowState, Screen, ScreenResult
from alt_ani_cli.shinden import episode as shinden_episode
from alt_ani_cli.shinden import search as shinden_search
from alt_ani_cli.shinden import series as shinden_series
from alt_ani_cli.shinden.models import SeriesRef
from alt_ani_cli.ui import menus, progress

# ---------------------------------------------------------------------------
# UI screens
# ---------------------------------------------------------------------------


def handle_start_mode(state: FlowState) -> ScreenResult:
    args = state.args
    # When launched with flags, skip the interactive menu
    if args.resume:
        return Screen.RESUME_PICK
    if args.url:
        ref = shinden_series.parse_series_url(args.url)
        state.ref = ref
        state.last_ep = 0.0
        return Screen.FETCH_EPISODES
    if args.query:
        state.query = " ".join(args.query)
        return Screen.SERIES_PICK

    all_entries = history.list_all()
    choice = menus.select_start_mode(
        has_history=bool(all_entries),
        history_count=len(all_entries),
    )
    if choice is None:
        return BACK  # ESC from first screen → exit via empty history_stack
    if choice == "quit":
        return None
    if choice == "resume":
        state.hits = []
        return Screen.RESUME_PICK
    if choice == "url":
        return Screen.URL_INPUT
    # "search"
    return Screen.SEARCH_QUERY


def handle_search_query(state: FlowState) -> ScreenResult:
    query = menus.prompt_search_query()
    if query is None:
        return BACK
    state.query = query
    return Screen.SERIES_PICK


def handle_url_input(state: FlowState) -> ScreenResult:
    url = menus.prompt_url()
    if url is None:
        return BACK
    ref = shinden_series.parse_series_url(url)
    state.ref = ref
    state.last_ep = 0.0
    return Screen.FETCH_EPISODES


def handle_resume_pick(state: FlowState) -> ScreenResult:
    all_entries = history.list_all()
    if not all_entries:
        progress.error("Historia jest pusta.")
        return BACK
    result = menus.select_series_from_history(all_entries)
    if result is None:
        return BACK
    state.ref, state.last_ep = result
    return Screen.FETCH_EPISODES


def handle_series_pick(state: FlowState) -> ScreenResult:
    if not state.query:
        return Screen.SEARCH_QUERY

    progress.info(f"Szukam: {state.query!r}")
    hits = shinden_search.search_series(state.client, state.query)
    if not hits:
        progress.error(f"Brak wyników dla: {state.query!r}")
        return BACK

    state.hits = hits
    hit = menus.select_series(hits)
    if hit is None:
        return BACK
    ref = shinden_series.parse_series_url(hit.url)
    state.ref = SeriesRef(id=ref.id, slug=ref.slug, title=hit.title, url=ref.url)
    state.last_ep = 0.0
    return Screen.FETCH_EPISODES


# ---------------------------------------------------------------------------
# Virtual screens (no UI)
# ---------------------------------------------------------------------------


def handle_fetch_episodes(state: FlowState) -> ScreenResult:
    assert state.ref is not None
    progress.info(f"Pobieram listę odcinków: {state.ref.title}")
    ref, episodes = shinden_series.list_episodes(state.client, state.ref)
    state.ref = ref
    state.episodes = episodes
    state.completed_eps = set()
    state.targets = []
    state.ep_idx = 0
    if not episodes:
        progress.error("Brak dostępnych odcinków.")
        return Screen.SERIES_PICK
    return Screen.EPISODES_PICK


# ---------------------------------------------------------------------------
# UI screens (continued)
# ---------------------------------------------------------------------------


def handle_episodes_pick(state: FlowState) -> ScreenResult:
    assert state.ref is not None
    args = state.args

    # When --episode was passed from CLI, skip the interactive menu
    if args.episode:
        from alt_ani_cli.cli import _parse_range

        targets = _parse_range(args.episode, state.episodes)
        if not targets:
            progress.error(f"Nie znaleziono odcinków dla zakresu: {args.episode!r}")
            return BACK
        state.targets = targets
        state.ep_idx = 0
        return Screen.EPISODE_DISPATCH

    if state.last_ep > 0:
        candidate = [ep for ep in state.episodes if ep.number > state.last_ep]
        pool = candidate if candidate else state.episodes
    else:
        pool = state.episodes

    result = menus.select_episodes(
        pool,
        prompt=f"Wybierz odcinek ({state.ref.title})",
        multi=True,
        watched_numbers=state.completed_eps,
    )
    if result is None:
        return BACK
    if not result:
        progress.warn("Nie wybrano żadnych odcinków.")
        return BACK
    state.targets = result
    state.ep_idx = 0
    return Screen.EPISODE_DISPATCH


def handle_episode_dispatch(state: FlowState) -> ScreenResult:
    if state.ep_idx >= len(state.targets):
        return None  # all episodes done

    ep = state.targets[state.ep_idx]
    progress.info(f"Odcinek {ep.number:g}: {ep.title}")

    ep_resp = state.client.get(ep.url)
    ep_resp.raise_for_status()

    args = state.args
    raw_players = shinden_episode.parse_players(ep_resp.text)
    players = shinden_episode.sort_players(raw_players, download=args.download)

    if not players:
        progress.warn(f"Brak playerów dla odcinka {ep.number:g} — pomijam.")
        state.ep_idx += 1
        return Screen.EPISODE_DISPATCH

    from alt_ani_cli.cli import _filter_players

    players = _filter_players(
        players,
        lang=args.lang,
        subs=args.subs,
        player_name=args.player_name,
    )

    state.players = players
    state.chosen_player = None
    state.failed_ids = set()
    state.stream = None
    state.embed = None

    if args.select_nth or len(players) == 1:
        state.chosen_player = players[0]
        return Screen.RESOLVE_STREAM

    return Screen.PLAYER_PICK


def handle_player_pick(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    ep_label = f"{ep.number:g}" if ep is not None else "?"

    if state.failed_ids:
        progress.warn(f"Player {state.chosen_player.player!r} nie zadziałał — wybierz inny:")

    chosen = menus.select_player(
        state.players,
        prompt=f"Player — ep {ep_label}",
        failed=state.failed_ids,
    )
    if chosen is None:
        return Screen.EPISODES_PICK  # ESC → back to episode selection
    state.chosen_player = chosen
    return Screen.RESOLVE_STREAM


def handle_resolve_stream(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    assert ep is not None

    from alt_ani_cli.cli import _resolve_with_fallback

    stream, embed = _resolve_with_fallback(
        state.client,
        state.players,
        state.chosen_player,
        auto=False,  # user already picked — try only chosen player
        ep_number=ep.number,
        cookies_file=state.args.cookies_file,
        cookies_browser=state.args.cookies_browser,
    )

    if stream is not None:
        state.stream = stream
        state.embed = embed
        if state.quality is None and stream.qualities:
            return Screen.QUALITY_PICK
        return Screen.ACTION_PICK

    # player failed
    state.failed_ids.add(state.chosen_player.online_id)
    remaining = [p for p in state.players if p.online_id not in state.failed_ids]
    if remaining:
        return Screen.PLAYER_PICK  # try another (no history push — stays in same UI level)

    progress.warn(f"Żaden player nie zadziałał dla odcinka {ep.number:g} — pomijam.")
    state.ep_idx += 1
    return Screen.EPISODE_DISPATCH


def handle_quality_pick(state: FlowState) -> ScreenResult:
    assert state.stream is not None
    quality = menus.select_quality(state.stream.qualities)
    if quality is None:
        state.quality = None  # reset cache so we ask again next time
        return Screen.PLAYER_PICK  # ESC → back to player selection
    state.quality = quality
    return Screen.ACTION_PICK


def handle_action_pick(state: FlowState) -> ScreenResult:
    args = state.args
    if args.download:
        state.episode_action = "download"
        return Screen.RUN_ACTION
    if args.debug:
        state.episode_action = "debug"
        return Screen.RUN_ACTION
    if state.episode_action is not None:
        return Screen.RUN_ACTION
    action = menus.select_action()
    if action is None:
        state.episode_action = None  # reset cache
        # ESC → back to quality pick if there were qualities, otherwise player
        if state.stream and state.stream.qualities:
            return Screen.QUALITY_PICK
        return Screen.PLAYER_PICK
    state.episode_action = action
    return Screen.RUN_ACTION


def handle_run_action(state: FlowState) -> ScreenResult:
    assert state.stream is not None
    assert state.ref is not None
    ep = state.current_ep
    assert ep is not None

    from alt_ani_cli.cli import _pick_quality, _print_debug

    args = state.args
    player_kind = "vlc" if args.vlc else "mpv"
    quality = state.quality or "best"
    stream = _pick_quality(state.stream, quality)
    title = f"{state.ref.title} — Odcinek {ep.number:g}"

    if args.download or state.episode_action == "download":
        download.run(stream, ep, state.ref)
    elif args.debug or state.episode_action == "debug":
        _print_debug(stream, state.embed)
    else:
        from alt_ani_cli.player import runner as player_runner

        player_runner.play(stream, kind=player_kind, title=title, no_detach=args.no_detach)
        progress.success(f"Odtwarzam w {player_kind}: {title}")

    history.upsert(state.ref, last_ep=ep.number)
    state.completed_eps.add(ep.number)
    state.ep_idx += 1
    state.stream = None
    state.embed = None
    return Screen.EPISODE_DISPATCH


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[Screen, Callable[[FlowState], ScreenResult]] = {
    Screen.START_MODE: handle_start_mode,
    Screen.SEARCH_QUERY: handle_search_query,
    Screen.URL_INPUT: handle_url_input,
    Screen.RESUME_PICK: handle_resume_pick,
    Screen.SERIES_PICK: handle_series_pick,
    Screen.FETCH_EPISODES: handle_fetch_episodes,
    Screen.EPISODES_PICK: handle_episodes_pick,
    Screen.EPISODE_DISPATCH: handle_episode_dispatch,
    Screen.PLAYER_PICK: handle_player_pick,
    Screen.RESOLVE_STREAM: handle_resolve_stream,
    Screen.QUALITY_PICK: handle_quality_pick,
    Screen.ACTION_PICK: handle_action_pick,
    Screen.RUN_ACTION: handle_run_action,
}
