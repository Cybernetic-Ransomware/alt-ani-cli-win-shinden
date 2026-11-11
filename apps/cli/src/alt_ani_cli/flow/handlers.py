"""FSM handlers — one function per Screen, driven by run_interactive.

Each handler receives FlowState, mutates it as needed, and returns the next
Screen, BACK (go one step back in history), or None (exit the application).

Handlers for virtual screens (FETCH_EPISODES, EPISODE_DISPATCH, RESOLVE_STREAM,
RUN_ACTION) perform I/O or computation and always return a concrete next Screen
(never BACK — the main loop never pushes them onto the history stack). They
contain no UI, with one exception: EPISODE_DISPATCH shows a confirm prompt when
player filters match nothing.
"""

import time
from collections.abc import Callable
from pathlib import Path

from alt_ani_core import diagnostics, download
from alt_ani_core.errors import (
    DownloadFailedError,
    DownloadTargetError,
    FreshResolveLimitError,
    ShindenError,
    ShindenXhrError,
    SourcesExhaustedError,
)
from alt_ani_core.models import (
    EpisodeRow,
    FallbackPolicy,
    PlayerEntry,
    Purpose,
    ResolvedStream,
    SeriesHit,
    SeriesMetadata,
    SeriesRef,
)
from alt_ani_core.rules.episode_range import parse_range
from alt_ani_core.rules.pin import find_pinned, player_fingerprint
from alt_ani_core.services.engine import Engine
from alt_ani_core.services.episode_session import EpisodeSession
from alt_ani_core.shinden import api as shinden_api
from alt_ani_core.shinden import episode as shinden_episode

from alt_ani_cli import __version__, playback
from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT, error_text
from alt_ani_cli.flow.state import _VIRTUAL_SCREENS, BACK, FlowState, Screen, ScreenResult, _BackSentinel
from alt_ani_cli.paths import CliPaths
from alt_ani_cli.player import runner as player_runner
from alt_ani_cli.ui import menus, progress

_PROG = CONTENT["progress"]
_M = CONTENT["menu"]
_EMPTY_META = SeriesMetadata(None, None, "", (), ())


def _prefetch_series_metadata(engine: Engine, hits: list[SeriesHit]) -> dict[str, SeriesMetadata]:
    if not hits:
        return {}
    refs: dict[str, SeriesRef] = {}
    for hit in hits:
        try:
            refs[hit.id] = engine.series_from_url(hit.url)
        except ShindenError:
            continue
    with progress.spinner(_M["series"]["loading_metadata"]):
        fetched = dict(zip(refs, engine.metadata_many(list(refs.values())), strict=True))
    failed_titles = [hit.title for hit in hits if fetched.get(hit.id) is None]
    if failed_titles:
        progress.warn(_M["series"]["metadata_fetch_failed"].format(titles=", ".join(failed_titles)))
    return {hit.id: fetched.get(hit.id) or _EMPTY_META for hit in hits}


def _session(state: FlowState, ep: EpisodeRow | None) -> EpisodeSession:
    session = state.session
    if ep is None or session is None or session.episode.url != ep.url:
        raise AssertionError("the episode session does not belong to the current episode")
    return session


def _chosen(state: FlowState) -> PlayerEntry:
    if state.chosen_player is None:
        raise AssertionError("no player is chosen")
    return state.chosen_player


def _reload_players(state: FlowState, ep: EpisodeRow) -> ScreenResult:
    if _session(state, ep).reload_used:
        progress.warn(_PROG["players_reload_limit"].format(number=ep.number))
        return Screen.PLAYER_PICK
    state.reload_pending = True
    progress.info(_PROG["players_reload"].format(number=ep.number))
    return Screen.EPISODE_DISPATCH


def _prefetch_player_sources(state: FlowState) -> shinden_api.XhrOutcome | None:
    session = _session(state, state.current_ep)
    known = session.known_sources
    players = [p for p in state.players if p.online_id not in known]
    if not players:
        return None
    with progress.spinner(_PROG["prefetch_sources"].format(count=len(players))):
        stop = session.probe_hosts(players)
    if stop is None:
        return None
    progress.warn(_PROG["prefetch_stopped"].format(reason=shinden_api.stop_message(stop)))
    return shinden_api.xhr_outcome(stop)


def _sorted_by_date_desc(hits: list[SeriesHit], metadata: dict[str, SeriesMetadata]) -> list[SeriesHit]:
    def key(h: SeriesHit):
        m = metadata.get(h.id)
        if m is None or m.air_date_sort is None:
            return (1, (0, 0, 0))
        y, mo, d = m.air_date_sort
        return (0, (-y, -mo, -d))

    return sorted(hits, key=key)


def _reset_download_resume(state: FlowState) -> None:
    """Leave download-resume mode so a newly picked series is not auto-downloaded."""
    if state.resume_mode == "download" and state.episode_action == "download":
        state.episode_action = None
    state.resume_mode = None
    state.downloaded_eps = set()


def _has_remaining_targets(state: FlowState) -> bool:
    return state.ep_idx < len(state.targets) - 1


def _first_missing_index(episodes: list[EpisodeRow], downloaded: set[float]) -> int:
    return next((i for i, ep in enumerate(episodes) if ep.number not in downloaded), len(episodes) - 1)


def handle_start_mode(state: FlowState) -> ScreenResult:
    args = state.options
    # When launched with flags, skip the interactive menu
    if args.resume:
        return Screen.DOWNLOAD_RESUME_PICK if args.download else Screen.RESUME_PICK
    if args.url:
        ref = state.engine.series_from_url(args.url)
        state.ref = ref
        state.last_ep = 0.0
        return Screen.FETCH_EPISODES
    if args.query:
        state.query = " ".join(args.query)
        return Screen.SERIES_PICK

    watch_count = len(state.engine.history.watch_entries())
    download_count = len(state.engine.history.download_entries())
    while True:
        choice = menus.select_start_mode(watch_count=watch_count, download_count=download_count)
        if choice != "version":
            break
        _sm = _M["start_mode"]
        menus.show_modal_text(_sm["version_header"], _sm["version_body"].format(version=__version__))
    if choice is None:
        return BACK  # ESC from first screen → exit via empty history_stack
    if choice == "quit":
        return None
    if choice == "resume_download":
        state.hits = []
        return Screen.DOWNLOAD_RESUME_PICK
    _reset_download_resume(state)
    if choice == "resume_watch":
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
    ref = state.engine.series_from_url(url)
    _reset_download_resume(state)
    state.ref = ref
    state.last_ep = 0.0
    return Screen.FETCH_EPISODES


def handle_resume_pick(state: FlowState) -> ScreenResult:
    all_entries = [(e.series, e.last_ep) for e in state.engine.history.watch_entries()]
    if not all_entries:
        progress.error(_PROG["history_empty"])
        return BACK
    result = menus.select_series_from_history(all_entries)
    if result is None:
        return BACK
    _reset_download_resume(state)
    state.ref, state.last_ep = result
    state.resume_mode = "watch"
    return Screen.FETCH_EPISODES


def handle_download_resume_pick(state: FlowState) -> ScreenResult:
    entries = [(e.series, e.episodes) for e in state.engine.history.download_entries()]
    if not entries:
        progress.error(_PROG["download_history_empty"])
        return BACK
    result = menus.select_series_from_download_history(entries)
    if result is None:
        return BACK
    ref, downloaded = result
    state.ref = ref
    state.last_ep = 0.0
    state.resume_mode = "download"
    state.downloaded_eps = set(downloaded)
    state.episode_action = "download"
    return Screen.FETCH_EPISODES


def handle_series_pick(state: FlowState) -> ScreenResult:
    if not state.query:
        return Screen.SEARCH_QUERY

    progress.info(_PROG["searching"].format(query=repr(state.query)))
    hits = state.engine.search(state.query)
    if not hits:
        progress.error(_PROG["no_results"].format(query=repr(state.query)))
        return BACK

    state.hits = hits
    metadata = _prefetch_series_metadata(state.engine, hits)

    original_hits = list(hits)
    current_hits = list(hits)
    sort_mode = "original"

    while True:
        signal = menus.select_series_once(current_hits, metadata=metadata)
        action = signal[0]
        payload = signal[1]

        if action == "back":
            return BACK

        if action == "pick":
            hit = payload
            ref = state.engine.series_from_url(hit.url)
            _reset_download_resume(state)
            state.ref = SeriesRef(id=ref.id, slug=ref.slug, title=hit.title, url=ref.url)
            state.last_ep = 0.0
            return Screen.FETCH_EPISODES

        cursor = payload if payload is not None else 0

        if action == "sort":
            prev_id = current_hits[cursor].id
            if sort_mode == "original":
                current_hits = _sorted_by_date_desc(current_hits, metadata)
                sort_mode = "date_desc"
            else:
                current_hits = list(original_hits)
                sort_mode = "original"
            cursor = next((i for i, h in enumerate(current_hits) if h.id == prev_id), 0)

        elif action == "desc":
            h = current_hits[cursor]
            m = metadata.get(h.id)
            menus.show_modal_text(
                _M["series"]["desc_header"].format(title=h.title),
                (m.description if m else "") or _M["series"]["desc_empty"],
            )

        elif action == "tags":
            h = current_hits[cursor]
            m = metadata.get(h.id)
            body = "\n".join(f"• {t}" for t in (m.tags if m else ())) or _M["series"]["tags_empty"]
            menus.show_modal_text(_M["series"]["tags_header"].format(title=h.title), body)

        elif action == "related":
            h = current_hits[cursor]
            m = metadata.get(h.id)
            picked = menus.pick_related(m.related if m else ())
            if picked is not None:
                new_hit = SeriesHit(
                    id=picked.id,
                    slug=picked.slug,
                    title=picked.title,
                    url=picked.url,
                    series_type="",
                )
                old_id = h.id
                current_hits[cursor] = new_hit
                for i, oh in enumerate(original_hits):
                    if oh.id == old_id:
                        original_hits[i] = new_hit
                        break
                metadata[new_hit.id] = _EMPTY_META


def handle_fetch_episodes(state: FlowState) -> ScreenResult:
    if state.ref is None:
        raise AssertionError
    progress.info(_PROG["fetching_episodes"].format(title=state.ref.title))
    listing = state.engine.episodes(state.ref)
    ref, episodes = listing.ref, listing.episodes
    state.ref = ref
    diagnostics.series_selected(ref.id, ref.title)
    state.episodes = episodes
    state.episodes_complete = listing.complete
    state.completed_eps = set()
    state.targets = []
    state.ep_idx = 0
    state.overwrite_existing_batch = False
    state.pinned_player = None
    state.episode_arg_consumed = False
    if not episodes:
        progress.error(_PROG["no_episodes"])
        return Screen.SERIES_PICK
    if not listing.complete:
        progress.warn(_PROG["episodes_incomplete"].format(fetched=listing.pages_fetched, total=listing.pages_total))
    return Screen.EPISODES_PICK


def handle_episodes_pick(state: FlowState) -> ScreenResult:
    if state.ref is None:
        raise AssertionError
    args = state.options

    # A partial list cannot resolve a range or a resume position; the user picks from what is visible.
    if args.episode and not state.episode_arg_consumed and not state.episodes_complete:
        progress.warn(_PROG["episodes_incomplete_range"].format(range=repr(args.episode)))
        state.episode_arg_consumed = True

    # --episode picks the first batch of a series only; later returns here must show the picker
    if args.episode and not state.episode_arg_consumed:
        targets = parse_range(args.episode, state.episodes)
        if targets is None:
            progress.warn(_PROG["range_unrecognized"].format(range=repr(args.episode)))
        if not targets:
            progress.error(_PROG["range_not_found"].format(range=repr(args.episode)))
            return BACK
        state.targets = targets
        state.ep_idx = 0
        state.overwrite_existing_batch = False
        state.pinned_player = None
        state.episode_arg_consumed = True
        return Screen.EPISODE_DISPATCH

    default_index = None
    watched = set(state.completed_eps)
    downloaded = set(state.downloaded_eps)
    if not state.episodes_complete:
        watched |= {ep.number for ep in state.episodes if ep.number <= state.last_ep}
    elif state.resume_mode == "download":
        default_index = _first_missing_index(state.episodes, downloaded)
    elif state.last_ep > 0:
        watched |= {ep.number for ep in state.episodes if ep.number <= state.last_ep}
        default_index = next(
            (i for i, ep in enumerate(state.episodes) if ep.number > state.last_ep),
            len(state.episodes) - 1,
        )

    result = menus.select_episodes(
        state.episodes,
        prompt=CONTENT["menu"]["episodes"]["prompt_with_title"].format(title=state.ref.title),
        multi=True,
        watched_numbers=watched,
        downloaded_numbers=downloaded,
        default_index=default_index,
    )
    if result is None:
        return BACK
    if not result:
        progress.warn(_PROG["no_episodes_picked"])
        return BACK
    state.targets = result
    state.ep_idx = 0
    state.overwrite_existing_batch = False
    state.pinned_player = None
    return Screen.EPISODE_DISPATCH


def handle_episode_dispatch(state: FlowState) -> ScreenResult:
    if state.ep_idx >= len(state.targets):
        return None  # all episodes done

    ep = state.targets[state.ep_idx]
    progress.info(_PROG["episode"].format(number=ep.number, title=ep.title))
    diagnostics.episode_selected(ep.number, ep.title, shinden_episode.episode_path(ep.url))

    reload_pending, state.reload_pending = state.reload_pending, False
    session, state.session = state.session, None
    if reload_pending and session is not None and session.episode.url == ep.url:
        session.reload()
    else:
        # a deliberately re-picked episode is a new session with fresh limits, as before
        session = state.engine.open_episode(ep)

    args = state.options
    download_mode = args.download or state.episode_action == "download"
    selection = session.select(args.source_filter, purpose=Purpose.DOWNLOAD if download_mode else Purpose.WATCH)
    players = list(selection.all_sources)

    if not players:
        progress.warn(_PROG["no_players"].format(number=ep.number))
        state.ep_idx += 1
        return Screen.EPISODE_DISPATCH

    if selection.unmatched:
        filters = ", ".join(selection.unmatched)
        if args.allow_fallback:
            progress.warn(_PROG["filter_fallback"].format(filters=filters))
        else:
            use_full = menus.confirm(_M["filter_confirm"]["question"].format(filters=filters))
            if not use_full:  # False or None (ESC) → back to episode selection
                state.pinned_player = None
                return Screen.EPISODES_PICK
    else:
        players = list(selection.candidates)

    state.players = players
    state.chosen_player = None
    state.player_picked_manually = False
    state.failed_ids = set()
    state.expired_ids = set()
    state.resolved = None
    state.session = session

    if args.select_nth:
        return _auto_pick_player(state, players[0])

    # matched on the final sorted/filtered list from the episode HTML alone — no resolve_embed needed
    if state.pinned_player is not None:
        pinned = find_pinned(players, state.pinned_player)
        if pinned is not None:
            return _auto_pick_player(state, pinned)
        # a missing pinned source needs a deliberate fallback, even with a single player left
        progress.info(_PROG["pin_no_match"].format(number=ep.number))
    elif len(players) == 1:
        return _auto_pick_player(state, players[0])

    if args.show_sources and _prefetch_player_sources(state) is shinden_api.XhrOutcome.PLAYERS_CHANGED:
        return _reload_players(state, ep)

    return Screen.PLAYER_PICK


def _auto_pick_player(state: FlowState, player: PlayerEntry) -> ScreenResult:
    state.chosen_player = player
    diagnostics.player_selected(player.online_id, player.player, None)
    return Screen.RESOLVE_STREAM


def handle_player_pick(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    ep_label = f"{ep.number:g}" if ep is not None else "?"

    if state.chosen_player is not None and state.chosen_player.online_id in state.failed_ids:
        progress.warn(_PROG["player_failed_short"].format(player=repr(state.chosen_player.player)))

    while True:
        sources = state.session.known_sources if state.session is not None else {}
        action, payload = menus.select_player_once(
            state.players,
            prompt=CONTENT["menu"]["player"]["prompt_with_episode"].format(label=ep_label),
            failed=state.failed_ids,
            sources=sources,
        )
        if action == "back":
            state.pinned_player = None  # leaving the batch
            return Screen.EPISODES_PICK  # ESC → back to episode selection
        if action == "pick":
            state.chosen_player = payload
            state.player_picked_manually = True
            source = sources.get(payload.online_id)
            diagnostics.player_selected(payload.online_id, payload.player, source.host if source else None)
            return Screen.RESOLVE_STREAM
        # "source" — show the modal, then re-render the picker
        p = state.players[payload]
        title, body = menus.format_player_source(p, sources.get(p.online_id))
        menus.show_modal_text(title, body)


def _resolve_chosen(state: FlowState, session: EpisodeSession, chosen: PlayerEntry) -> ResolvedStream:
    """A person picked this source, so only it is tried; one that already failed downstream gets a fresh embed."""
    options = state.options.extract_options
    if chosen.online_id in state.expired_ids:
        try:
            return session.resolve(chosen, policy=FallbackPolicy.SINGLE, options=options, force_fresh=True)
        except FreshResolveLimitError:
            pass
    return session.resolve(chosen, policy=FallbackPolicy.SINGLE, options=options)


def handle_resolve_stream(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    chosen = state.chosen_player
    if ep is None or chosen is None:
        raise AssertionError

    try:
        resolved = _resolve_chosen(state, _session(state, ep), chosen)
    except ShindenXhrError as exc:
        # shared-API failure, not this player's: keep it pickable and leave any retry to the user
        progress.error(shinden_api.stop_message(exc))
        if shinden_api.xhr_outcome(exc) is shinden_api.XhrOutcome.PLAYERS_CHANGED:
            # the shown list is stale: reload this episode's page instead of offering ids that are gone
            return _reload_players(state, ep)
        return Screen.PLAYER_PICK
    except SourcesExhaustedError:
        return _fail_chosen_player(state, ep)

    state.resolved = resolved
    if state.quality is None and resolved.stream.labels:
        return Screen.QUALITY_PICK
    return Screen.ACTION_PICK


def _fail_chosen_player(state: FlowState, ep: EpisodeRow) -> ScreenResult:
    if state.chosen_player is None:
        raise AssertionError
    state.failed_ids.add(state.chosen_player.online_id)
    remaining = [p for p in state.players if p.online_id not in state.failed_ids]
    if remaining:
        return Screen.PLAYER_PICK  # try another (no history push — stays in same UI level)

    progress.warn(_PROG["no_player_worked"].format(number=ep.number))
    state.ep_idx += 1
    return Screen.EPISODE_DISPATCH


def handle_quality_pick(state: FlowState) -> ScreenResult:
    if state.resolved is None:
        raise AssertionError
    quality = menus.select_quality(state.resolved.stream.labels)
    if quality is None:
        state.quality = None  # reset cache so we ask again next time
        return Screen.PLAYER_PICK  # ESC → back to player selection
    state.quality = quality
    return Screen.ACTION_PICK


def handle_action_pick(state: FlowState) -> ScreenResult:
    args = state.options
    if args.download:
        state.episode_action = "download"
        return Screen.RUN_ACTION
    if args.debug:
        state.episode_action = "debug"
        return Screen.RUN_ACTION
    if state.episode_action is not None:
        if state.episode_action == "download" and _offers_repin(state):
            choice = menus.select_pin_fallback_action()
            if choice is None:
                return _back_from_action(state)
            if choice == "repin":
                state.pinned_player = player_fingerprint(_chosen(state))
        return Screen.RUN_ACTION
    action = menus.select_action(offer_pin=_has_remaining_targets(state))
    if action is None:
        state.episode_action = None  # reset cache
        return _back_from_action(state)
    if action == "download_pin":
        state.pinned_player = player_fingerprint(_chosen(state))
        action = "download"
    state.episode_action = action
    return Screen.RUN_ACTION


def _offers_repin(state: FlowState) -> bool:
    """A manual fallback must not silently replace the pin, so ask when the picked player differs from it."""
    return (
        state.pinned_player is not None
        and state.player_picked_manually
        and _has_remaining_targets(state)
        and player_fingerprint(_chosen(state)) != state.pinned_player
    )


def _back_from_action(state: FlowState) -> ScreenResult:
    if state.resolved is not None and state.resolved.stream.labels:
        return Screen.QUALITY_PICK
    return Screen.PLAYER_PICK


def handle_run_action(state: FlowState) -> ScreenResult:
    resolved, ep, chosen = state.resolved, state.current_ep, state.chosen_player
    if resolved is None or state.ref is None or ep is None or chosen is None:
        raise AssertionError

    args = state.options
    history = state.engine.history
    variant = resolved.stream.select(state.quality or "best")
    title = playback.episode_title(state.ref.title, ep.number)

    completed = False
    if args.download or state.episode_action == "download":
        host = diagnostics.host_of_url(resolved.embed.url).removeprefix("www.")
        existing: dict = {}

        def confirm_overwrite(path: Path) -> bool:
            if state.overwrite_existing_batch:
                return True
            choice = menus.select_existing_download_action(str(path), has_remaining=_has_remaining_targets(state))
            existing.update(choice=choice, path=path)
            if choice == "overwrite_remaining":
                state.overwrite_existing_batch = True
            return choice in ("overwrite", "overwrite_remaining")

        try:
            downloaded = download.run(
                variant,
                ep,
                state.ref,
                state.paths.downloads_dir,
                confirm_overwrite=confirm_overwrite,
                events=state.engine.events,
            )
        except DownloadFailedError as exc:
            diagnostics.download_result(host, ok=False, exc=type(exc).__name__)
            progress.error(_PROG["download_failed"].format(player=repr(chosen.player)))
            state.resolved = None
            state.expired_ids.add(chosen.online_id)
            return _fail_chosen_player(state, ep)
        except DownloadTargetError as exc:
            # A local file problem (e.g. the old file is open in a player): another source would hit it too.
            progress.error(error_text(exc))
            state.resolved = None
            return Screen.PLAYER_PICK
        if not downloaded and existing["choice"] == "cancel":
            state.overwrite_existing_batch = False
            state.pinned_player = None
            state.resolved = None
            progress.warn(CONTENT["download"]["batch_cancelled"])
            return Screen.EPISODES_PICK
        if downloaded:
            diagnostics.download_result(host, ok=True, exc=None)
        else:
            progress.info(CONTENT["download"]["kept_existing"].format(path=existing["path"]))
        history.record_download(state.ref, ep.number)
        state.downloaded_eps.add(ep.number)
    elif args.debug or state.episode_action == "debug":
        playback.print_debug(resolved.stream, variant, resolved.embed)
    else:
        result = player_runner.play(variant, kind=args.player_kind, title=title, no_detach=args.no_detach)
        playback.report_playback(result, args, title)
        completed = playback.playback_confirmed(result, args.no_detach)

    if completed:
        history.record_watch(state.ref, ep.number)
        state.completed_eps.add(ep.number)
        diagnostics.history_update(state.ref.id, ep.number)
    state.ep_idx += 1
    state.resolved = None
    return Screen.EPISODE_DISPATCH


HANDLERS: dict[Screen, Callable[[FlowState], ScreenResult]] = {
    Screen.START_MODE: handle_start_mode,
    Screen.SEARCH_QUERY: handle_search_query,
    Screen.URL_INPUT: handle_url_input,
    Screen.RESUME_PICK: handle_resume_pick,
    Screen.DOWNLOAD_RESUME_PICK: handle_download_resume_pick,
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


def run_interactive(options: CliOptions, engine: Engine, cli_paths: CliPaths) -> None:
    state = FlowState(options=options, engine=engine, paths=cli_paths, quality=options.quality)

    history_stack: list[Screen] = []
    screen: Screen | None = Screen.START_MODE

    while screen is not None:
        current = screen
        start = time.monotonic()
        result = HANDLERS[screen](state)
        elapsed = time.monotonic() - start
        if isinstance(result, _BackSentinel):
            screen = None if not history_stack else history_stack.pop()
            diagnostics.screen_transition(current.name, screen.name if screen else "EXIT", elapsed)
        elif result is None:
            screen = None
            diagnostics.screen_transition(current.name, "EXIT", elapsed)
        else:
            if screen not in _VIRTUAL_SCREENS:
                history_stack.append(screen)
            screen = result
            diagnostics.screen_transition(current.name, screen.name, elapsed)
