"""Turns engine events into terminal output; the engine itself never prints."""

import shutil
from contextlib import AbstractContextManager

from alt_ani_core import events as ev
from alt_ani_core.services.episode_session import PHASE_ANTIBOT

from alt_ani_cli.content import CONTENT
from alt_ani_cli.ui import progress

_PROG = CONTENT["progress"]
_DL = CONTENT["download"]

ANTIBOT_LABEL = "antibot delay"


def _truncated(line: str) -> str:
    cols = shutil.get_terminal_size((120, 24)).columns
    return line[: cols - 1] + "…" if len(line) > cols else line


class ConsoleEvents:
    def __init__(self) -> None:
        self._spinners: dict[str, AbstractContextManager[None]] = {}

    def emit(self, event: ev.EngineEvent) -> None:  # noqa: C901
        match event:
            case ev.PhaseStarted(phase=phase):
                if phase == PHASE_ANTIBOT and phase not in self._spinners:
                    spinner = progress.spinner(_PROG["spinner_api"].format(label=ANTIBOT_LABEL))
                    spinner.__enter__()
                    self._spinners[phase] = spinner
            case ev.PhaseFinished(phase=phase):
                spinner = self._spinners.pop(phase, None)
                if spinner is not None:
                    spinner.__exit__(None, None, None)
            case ev.ClearanceRequested():
                progress.info(_PROG["clearance_requested"])
            case ev.XhrFallback(episode=number, reason=reason):
                progress.warn(_PROG["xhr_fallback_api4"].format(number=number, reason=reason))
            case ev.EmbedResolved(redacted_url=url):
                progress.info(_PROG["embed"].format(url=url))
            case ev.SourceAttemptFailed(player=player, episode=number, redacted_text=text):
                progress.warn(_PROG["player_failed_long"].format(player=repr(player), number=number, exc=text))
            case ev.HostDeferred(host=host):
                progress.warn(_PROG["host_deferred"].format(host=host))
            case ev.HostLastResort(host=host):
                progress.warn(_PROG["host_last_resort"].format(host=host))
            case ev.HostDropped(host=host):
                progress.warn(_PROG["host_dropped"].format(host=host))
            case ev.ExtractorFallback(kind=kind, host=host, redacted_text=text):
                progress.warn(_PROG[kind].format(host=host, exc=text))
            case ev.DownloadStarted(title=title, episode_label=label, directory=directory):
                progress.info(_DL["starting"].format(title=title, ep_label=label, dir=directory))
            case ev.DownloadLog(redacted_line=line):
                print(_truncated(line))
            case ev.LegacyPartMoved(path=path):
                progress.warn(_DL["legacy_part_moved"].format(path=path))
            case ev.DownloadSaved(path=path):
                progress.success(_DL["saved"].format(path=path))
            case _:
                pass
