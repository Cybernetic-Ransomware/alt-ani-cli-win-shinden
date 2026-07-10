"""FSM handlers — one function per Screen, called by the main loop in cli.py.

Each handler receives FlowState, mutates it as needed, and returns the next
Screen, BACK (go one step back in history), or None (exit the application).

Handlers for virtual screens (FETCH_EPISODES, EPISODE_DISPATCH, RESOLVE_STREAM,
RUN_ACTION) perform I/O or computation and always return a concrete next Screen
(never BACK — the main loop never pushes them onto the history stack). They
contain no UI, with one exception: EPISODE_DISPATCH shows a confirm prompt when
player filters match nothing.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

from alt_ani_cli import download, history
from alt_ani_cli.content import CONTENT
from alt_ani_cli.errors import ShindenError
from alt_ani_cli.flow.state import BACK, FlowState, Screen, ScreenResult
from alt_ani_cli.models import EmbedURL, PlayerSource, SeriesHit, SeriesMetadata, SeriesRef
from alt_ani_cli.shinden import api as shinden_api
from alt_ani_cli.shinden import episode as shinden_episode
from alt_ani_cli.shinden import search as shinden_search
from alt_ani_cli.shinden import series as shinden_series
from alt_ani_cli.shinden.metadata import fetch_series_metadata
from alt_ani_cli.shinden.series import parse_series_url
from alt_ani_cli.ui import menus, progress

_PROG = CONTENT["progress"]
_M = CONTENT["menu"]
_EMPTY_META = SeriesMetadata(None, None, "", (), ())


def _prefetch_series_metadata(client: cffi_requests.Session, hits: list[SeriesHit]) -> dict[str, SeriesMetadata]:
    if not hits:
        return {}
    out: dict[str, SeriesMetadata] = {}
    failed_titles: list[str] = []
    with progress.spinner(_M["series"]["loading_metadata"]), ThreadPoolExecutor(max_workers=min(8, len(hits))) as ex:
        futures = {ex.submit(_safe_fetch_one, client, h): h for h in hits}
        for fut in as_completed(futures):
            h = futures[fut]
            try:
                out[h.id] = fut.result()
            except CurlRequestException, ShindenError:
                out[h.id] = _EMPTY_META
                failed_titles.append(h.title)
    if failed_titles:
        progress.warn(_M["series"]["metadata_fetch_failed"].format(titles=", ".join(failed_titles)))
    return out


def _safe_fetch_one(client: cffi_requests.Session, hit: SeriesHit) -> SeriesMetadata:
    ref = parse_series_url(hit.url)
    return fetch_series_metadata(client, ref)


def _record_player_source(state: FlowState, online_id: str, embed: EmbedURL) -> None:
    host = (urlparse(embed.url).hostname or "").removeprefix("www.")
    state.player_embeds[online_id] = embed
    state.player_sources[online_id] = PlayerSource(online_id=online_id, host=host, embed_url=embed.url)


def _prefetch_player_sources(state: FlowState) -> None:
    """Resolve every player's embed up front (--show-sources); failures leave the host unknown."""
    players = [p for p in state.players if p.online_id not in state.player_sources]
    if not players:
        return
    with (
        progress.spinner(_PROG["prefetch_sources"].format(count=len(players))),
        ThreadPoolExecutor(max_workers=min(3, len(players))) as ex,
    ):
        futures = {ex.submit(shinden_api.resolve_embed, state.client, p.online_id): p for p in players}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                embed = fut.result()
            except CurlRequestException, ShindenError:
                continue
            _record_player_source(state, p.online_id, embed)


def _sorted_by_date_desc(hits: list[SeriesHit], metadata: dict[str, SeriesMetadata]) -> list[SeriesHit]:
    def key(h: SeriesHit):
        m = metadata.get(h.id)
        if m is None or m.air_date_sort is None:
            return (1, (0, 0, 0))
        y, mo, d = m.air_date_sort
        return (0, (-y, -mo, -d))

    return sorted(hits, key=key)


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
        progress.error(_PROG["history_empty"])
        return BACK
    result = menus.select_series_from_history(all_entries)
    if result is None:
        return BACK
    state.ref, state.last_ep = result
    return Screen.FETCH_EPISODES


def handle_series_pick(state: FlowState) -> ScreenResult:
    if not state.query:
        return Screen.SEARCH_QUERY

    progress.info(_PROG["searching"].format(query=repr(state.query)))
    hits = shinden_search.search_series(state.client, state.query)
    if not hits:
        progress.error(_PROG["no_results"].format(query=repr(state.query)))
        return BACK

    state.hits = hits
    metadata = _prefetch_series_metadata(state.client, hits)

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
            ref = shinden_series.parse_series_url(hit.url)
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
    ref, episodes = shinden_series.list_episodes(state.client, state.ref)
    state.ref = ref
    state.episodes = episodes
    state.completed_eps = set()
    state.targets = []
    state.ep_idx = 0
    if not episodes:
        progress.error(_PROG["no_episodes"])
        return Screen.SERIES_PICK
    return Screen.EPISODES_PICK


def handle_episodes_pick(state: FlowState) -> ScreenResult:
    if state.ref is None:
        raise AssertionError
    args = state.args

    # When --episode was passed from CLI, skip the interactive menu
    if args.episode:
        from alt_ani_cli.cli import _parse_range

        targets = _parse_range(args.episode, state.episodes)
        if not targets:
            progress.error(_PROG["range_not_found"].format(range=repr(args.episode)))
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
        prompt=CONTENT["menu"]["episodes"]["prompt_with_title"].format(title=state.ref.title),
        multi=True,
        watched_numbers=state.completed_eps,
    )
    if result is None:
        return BACK
    if not result:
        progress.warn(_PROG["no_episodes_picked"])
        return BACK
    state.targets = result
    state.ep_idx = 0
    return Screen.EPISODE_DISPATCH


def handle_episode_dispatch(state: FlowState) -> ScreenResult:
    if state.ep_idx >= len(state.targets):
        return None  # all episodes done

    ep = state.targets[state.ep_idx]
    progress.info(_PROG["episode"].format(number=ep.number, title=ep.title))

    ep_resp = state.client.get(ep.url)
    ep_resp.raise_for_status()

    args = state.args
    raw_players = shinden_episode.parse_players(ep_resp.text)
    players = shinden_episode.sort_players(raw_players, download=args.download)

    if not players:
        progress.warn(_PROG["no_players"].format(number=ep.number))
        state.ep_idx += 1
        return Screen.EPISODE_DISPATCH

    from alt_ani_cli.cli import _filter_players

    filtered, unmatched = _filter_players(
        players,
        lang=args.lang,
        subs=args.subs,
        player_name=args.player_name,
    )
    if unmatched:
        filters = ", ".join(unmatched)
        if args.allow_fallback:
            progress.warn(_PROG["filter_fallback"].format(filters=filters))
        else:
            use_full = menus.confirm(_M["filter_confirm"]["question"].format(filters=filters))
            if not use_full:  # False or None (ESC) → back to episode selection
                return Screen.EPISODES_PICK
    else:
        players = filtered

    state.players = players
    state.chosen_player = None
    state.failed_ids = set()
    state.stream = None
    state.embed = None
    state.player_sources = {}
    state.player_embeds = {}

    if args.select_nth or len(players) == 1:
        state.chosen_player = players[0]
        return Screen.RESOLVE_STREAM

    if args.show_sources:
        _prefetch_player_sources(state)

    return Screen.PLAYER_PICK


def handle_player_pick(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    ep_label = f"{ep.number:g}" if ep is not None else "?"

    if state.failed_ids:
        progress.warn(_PROG["player_failed_short"].format(player=repr(state.chosen_player.player)))

    while True:
        action, payload = menus.select_player_once(
            state.players,
            prompt=CONTENT["menu"]["player"]["prompt_with_episode"].format(label=ep_label),
            failed=state.failed_ids,
            sources=state.player_sources,
        )
        if action == "back":
            return Screen.EPISODES_PICK  # ESC → back to episode selection
        if action == "pick":
            state.chosen_player = payload
            return Screen.RESOLVE_STREAM
        # "source" — show the modal, then re-render the picker
        p = state.players[payload]
        title, body = menus.format_player_source(p, state.player_sources.get(p.online_id))
        menus.show_modal_text(title, body)


def handle_resolve_stream(state: FlowState) -> ScreenResult:
    ep = state.current_ep
    if ep is None:
        raise AssertionError

    from alt_ani_cli.cli import _resolve_with_fallback

    stream, embed = _resolve_with_fallback(
        state.client,
        state.players,
        state.chosen_player,
        auto=False,  # user already picked — try only chosen player
        ep_number=ep.number,
        cookies_file=state.args.cookies_file,
        cookies_browser=state.args.cookies_browser,
        embed_cache=state.player_embeds,
    )

    if stream is not None:
        state.stream = stream
        state.embed = embed
        # opportunistic: a re-rendered player list can now show this player's real host
        _record_player_source(state, state.chosen_player.online_id, embed)
        if state.quality is None and stream.qualities:
            return Screen.QUALITY_PICK
        return Screen.ACTION_PICK

    # player failed
    state.failed_ids.add(state.chosen_player.online_id)
    remaining = [p for p in state.players if p.online_id not in state.failed_ids]
    if remaining:
        return Screen.PLAYER_PICK  # try another (no history push — stays in same UI level)

    progress.warn(_PROG["no_player_worked"].format(number=ep.number))
    state.ep_idx += 1
    return Screen.EPISODE_DISPATCH


def handle_quality_pick(state: FlowState) -> ScreenResult:
    if state.stream is None:
        raise AssertionError
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
    if state.stream is None:
        raise AssertionError
    if state.ref is None:
        raise AssertionError
    ep = state.current_ep
    if ep is None:
        raise AssertionError

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
        progress.success(_PROG["playing"].format(kind=player_kind, title=title))

    history.upsert(state.ref, last_ep=ep.number)
    state.completed_eps.add(ep.number)
    state.ep_idx += 1
    state.stream = None
    state.embed = None
    return Screen.EPISODE_DISPATCH


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
