"""Desktop playback reporting: what counts as started, failed or confirmed is a property of mpv/VLC, not of the engine."""

from alt_ani_cli.args import CliOptions
from alt_ani_cli.content import CONTENT
from alt_ani_cli.player import mpv
from alt_ani_cli.player.runner import PlayResult
from alt_ani_cli.ui import progress
from alt_ani_core import diagnostics
from alt_ani_core.models import EmbedURL, Stream, StreamVariant
from alt_ani_core.redaction import redact_headers, redact_text

_PROG = CONTENT["progress"]

# rc == 0 this fast rarely means real playback — mpv.net forwards the URL to an
# already-running window over IPC and exits instantly regardless of what happened next.
_FAST_EXIT_THRESHOLD_SEC = 2.0


def episode_title(series_title: str, number: float) -> str:
    return f"{series_title} — Odcinek {number:g}"


def playback_confirmed(result: PlayResult, no_detach: bool) -> bool:
    """Whether playback should count toward watch history — same verdict as report_playback."""
    if not no_detach:
        return True  # detached mode can't measure rc/elapsed; keep the prior behavior
    return result.rc == 0 and result.elapsed >= _FAST_EXIT_THRESHOLD_SEC


def report_playback(result: PlayResult, options: CliOptions, title: str) -> None:
    kind = options.player_kind
    if not options.no_detach:
        progress.success(_PROG["playing"].format(kind=kind, title=title))
    elif result.rc != 0:
        progress.error(_PROG["playing_failed"].format(kind=kind, title=title, rc=result.rc))
    elif result.elapsed < _FAST_EXIT_THRESHOLD_SEC:
        progress.warn(_PROG["playing_unconfirmed"].format(kind=kind, title=title, secs=result.elapsed))
    else:
        progress.success(_PROG["playing"].format(kind=kind, title=title))

    mpv_log: str | None = None
    if kind == "mpv":
        log_file = mpv.log_file()
        mpv_log = str(log_file)
        progress.info(_PROG["mpv_log_hint"].format(path=log_file))

    diagnostics.playback_result(
        kind=kind,
        rc=result.rc,
        elapsed=result.elapsed,
        confirmed=playback_confirmed(result, options.no_detach),
        mpv_log=mpv_log,
    )


def print_debug(stream: Stream, chosen: StreamVariant, embed: EmbedURL) -> None:
    from rich.table import Table

    con = progress._get()
    con.print(f"\n[bold]Embed URL:[/bold] {redact_text(embed.url)}")
    con.print(f"[bold]Direct URL:[/bold] {redact_text(chosen.url)}")
    con.print(f"[bold]Protocol:[/bold] {chosen.protocol.value}")
    con.print(f"[bold]Ext:[/bold] {chosen.container or 'm3u8'}")

    if chosen.headers:
        t = Table("Header", "Value", title="HTTP headers")
        for k, v in redact_headers(dict(chosen.headers)).items():
            t.add_row(k, v)
        con.print(t)

    labelled = [v for v in stream.variants if v.label]
    if labelled:
        t = Table("Quality", "URL", title="Available qualities")
        for variant in sorted(labelled, key=lambda v: v.label):
            u = redact_text(variant.url)
            t.add_row(variant.label, u[:80] + "..." if len(u) > 80 else u)
        con.print(t)
