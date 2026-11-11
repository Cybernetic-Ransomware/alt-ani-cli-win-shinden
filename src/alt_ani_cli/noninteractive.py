"""Unattended run: first match, health-ordered sources, automatic fallback down the list."""

import sys

from alt_ani_core import diagnostics, download
from alt_ani_core.errors import FilterMismatchError, IncompleteEpisodeListError, SourcesExhaustedError
from alt_ani_core.models import FallbackPolicy, Purpose, SeriesRef
from alt_ani_core.rules.episode_range import parse_range
from alt_ani_core.services.engine import Engine
from alt_ani_core.shinden import episode as shinden_episode

from alt_ani_cli import playback
from alt_ani_cli.args import CliOptions, missing_input_error
from alt_ani_cli.content import CONTENT, EXCEPTIONS
from alt_ani_cli.paths import CliPaths
from alt_ani_cli.player import runner as player_runner
from alt_ani_cli.ui import progress

_PROG = CONTENT["progress"]


def _pick_series(options: CliOptions, engine: Engine) -> tuple[SeriesRef, float, frozenset[float]]:
    if options.resume and options.download:
        downloads = engine.history.download_entries()
        if not downloads:
            progress.error(_PROG["download_history_empty"])
            sys.exit(1)
        return downloads[0].series, 0.0, downloads[0].episodes
    if options.resume:
        watched = engine.history.watch_entries()
        if not watched:
            progress.error(_PROG["history_empty"])
            sys.exit(1)
        return watched[0].series, watched[0].last_ep, frozenset()
    if options.url:
        return engine.series_from_url(options.url), 0.0, frozenset()

    query = " ".join(options.query)
    progress.info(_PROG["searching"].format(query=repr(query)))
    hits = engine.search(query)
    if not hits:
        progress.error(_PROG["no_results"].format(query=repr(query)))
        sys.exit(1)

    idx = (options.select_nth - 1) if options.select_nth else 0
    if idx < 0 or idx >= len(hits):
        progress.error(_PROG["select_nth_invalid"].format(n=options.select_nth, count=len(hits)))
        sys.exit(1)
    ref = engine.series_from_url(hits[idx].url)
    return SeriesRef(id=ref.id, slug=ref.slug, title=hits[idx].title, url=ref.url), 0.0, frozenset()


def run(options: CliOptions, engine: Engine, cli_paths: CliPaths) -> None:  # noqa: C901
    if not options.query and not options.url and not options.resume:
        missing_input_error()

    ref, last_ep, downloaded_eps = _pick_series(options, engine)

    progress.info(_PROG["fetching_episodes"].format(title=ref.title))
    listing = engine.episodes(ref)
    ref, episodes = listing.ref, listing.episodes
    diagnostics.series_selected(ref.id, ref.title)

    # -e, -c, download resume and the default first episode all treat the list as the full catalogue.
    if not listing.complete:
        raise IncompleteEpisodeListError(
            EXCEPTIONS["shinden"]["episodes_incomplete"].format(
                title=ref.title,
                fetched=listing.pages_fetched,
                total=listing.pages_total,
                reason=listing.incomplete_reason,
            )
        )

    if not episodes:
        progress.error(_PROG["no_episodes"])
        sys.exit(1)

    if options.episode:
        targets = parse_range(options.episode, episodes)
        if targets is None:
            progress.warn(_PROG["range_unrecognized"].format(range=repr(options.episode)))
        if not targets:
            progress.error(_PROG["range_not_found"].format(range=repr(options.episode)))
            sys.exit(1)
    elif options.resume and options.download:
        remaining = [ep for ep in episodes if ep.number not in downloaded_eps]
        if not remaining:
            progress.info(_PROG["downloaded_all"].format(title=ref.title))
            return
        targets = [remaining[0]]
    elif options.resume and last_ep > 0:
        remaining = [ep for ep in episodes if ep.number > last_ep]
        if not remaining:
            progress.warn(_PROG["watched_all"].format(title=ref.title))
            remaining = episodes
        targets = [remaining[0]]
    else:
        targets = [episodes[0]]

    purpose = Purpose.DOWNLOAD if options.download else Purpose.WATCH

    for ep in targets:
        progress.info(_PROG["episode"].format(number=ep.number, title=ep.title))
        diagnostics.episode_selected(ep.number, ep.title, shinden_episode.episode_path(ep.url))

        session = engine.open_episode(ep)
        selection = session.select(options.source_filter, purpose=purpose)
        players = selection.all_sources

        if not players:
            progress.warn(_PROG["no_players"].format(number=ep.number))
            continue

        if selection.unmatched:
            filters = ", ".join(selection.unmatched)
            if not options.allow_fallback:
                raise FilterMismatchError(EXCEPTIONS["cli"]["filter_no_match"].format(filters=filters))
            progress.warn(_PROG["filter_fallback"].format(filters=filters))
        else:
            players = selection.candidates

        ordered = session.rank(players)
        try:
            resolved = session.resolve(
                ordered[0], candidates=ordered, policy=FallbackPolicy.WALK, options=options.extract_options
            )
        except SourcesExhaustedError:
            progress.warn(_PROG["no_player_worked"].format(number=ep.number))
            continue

        variant = resolved.stream.select(options.quality or "best")
        title = playback.episode_title(ref.title, ep.number)

        if options.download:
            download.run(variant, ep, ref, cli_paths.downloads_dir, events=engine.events)
            engine.history.record_download(ref, ep.number)
        elif options.debug:
            playback.print_debug(resolved.stream, variant, resolved.embed)
        else:
            result = player_runner.play(variant, kind=options.player_kind, title=title, no_detach=options.no_detach)
            playback.report_playback(result, options, title)
            if playback.playback_confirmed(result, options.no_detach):
                engine.history.record_watch(ref, ep.number)
