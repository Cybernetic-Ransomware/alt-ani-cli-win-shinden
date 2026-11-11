import os
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from alt_ani_core import download, events
from alt_ani_core.services.episode_session import PHASE_ANTIBOT

from alt_ani_cli import event_renderer
from alt_ani_cli.content import CONTENT
from alt_ani_cli.event_renderer import ANTIBOT_LABEL, ConsoleEvents

_PROG = CONTENT["progress"]
_DL = CONTENT["download"]
_CHANNELS = ("info", "warn", "success", "error", "output", "rule")


class _Console:
    """Stands in for alt_ani_cli.ui.progress: records every call the renderer makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def channel(self, name: str):
        return lambda message: self.calls.append((name, message))

    @contextmanager
    def spinner(self, message: str):
        self.calls.append(("spinner_enter", message))
        try:
            yield
        finally:
            self.calls.append(("spinner_exit", message))


@pytest.fixture
def console(monkeypatch) -> _Console:
    recorder = _Console()
    for name in _CHANNELS:
        monkeypatch.setattr(event_renderer.progress, name, recorder.channel(name))
    monkeypatch.setattr(event_renderer.progress, "spinner", recorder.spinner)
    return recorder


def _terminal(columns: int):
    return patch("alt_ani_cli.event_renderer.shutil.get_terminal_size", return_value=os.terminal_size((columns, 24)))


_SPINNER_TEXT = _PROG["spinner_api"].format(label=ANTIBOT_LABEL)


@pytest.mark.unit
class TestPhaseSpinner:
    def test_antibot_phase_enters_and_exits_the_spinner_exactly_once(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted(PHASE_ANTIBOT))
        assert console.calls == [("spinner_enter", _SPINNER_TEXT)]
        renderer.emit(events.PhaseFinished(PHASE_ANTIBOT))
        assert console.calls == [("spinner_enter", _SPINNER_TEXT), ("spinner_exit", _SPINNER_TEXT)]

    def test_phase_name_is_the_one_the_engine_emits(self):
        assert PHASE_ANTIBOT == "antibot_delay"

    def test_repeated_start_does_not_open_a_second_spinner(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted("antibot_delay"))
        renderer.emit(events.PhaseStarted("antibot_delay"))
        renderer.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter", "spinner_exit"]

    def test_repeated_finish_does_not_exit_twice(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted("antibot_delay"))
        renderer.emit(events.PhaseFinished("antibot_delay"))
        renderer.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter", "spinner_exit"]

    def test_finish_without_start_is_ignored(self, console):
        ConsoleEvents().emit(events.PhaseFinished("antibot_delay"))
        assert console.calls == []

    def test_each_wait_gets_its_own_spinner(self, console):
        renderer = ConsoleEvents()
        for _ in range(2):
            renderer.emit(events.PhaseStarted("antibot_delay"))
            renderer.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter", "spinner_exit"] * 2

    def test_unknown_phase_shows_nothing(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted("some_future_phase"))
        renderer.emit(events.PhaseFinished("some_future_phase"))
        assert console.calls == []

    def test_messages_inside_the_phase_are_rendered_between_enter_and_exit(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted("antibot_delay"))
        renderer.emit(events.HostDeferred(host="cdn.example"))
        renderer.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter", "warn", "spinner_exit"]

    def test_spinners_are_per_renderer(self, console):
        first, second = ConsoleEvents(), ConsoleEvents()
        first.emit(events.PhaseStarted("antibot_delay"))
        second.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter"]


@pytest.mark.unit
class TestRenderedMessages:
    @pytest.mark.parametrize(
        ("event", "call"),
        [
            (events.ClearanceRequested(), ("info", _PROG["clearance_requested"])),
            (
                events.XhrFallback(episode=5.0, reason="missing_token"),
                ("warn", _PROG["xhr_fallback_api4"].format(number=5.0, reason="missing_token")),
            ),
            (
                events.EmbedResolved(redacted_url="https://embed.example/e/1?token=<redacted>"),
                ("info", _PROG["embed"].format(url="https://embed.example/e/1?token=<redacted>")),
            ),
            (
                events.SourceAttemptFailed(player="CDA", episode=5.5, redacted_text="no stream"),
                ("warn", _PROG["player_failed_long"].format(player="'CDA'", number=5.5, exc="no stream")),
            ),
            (events.HostDeferred(host="cdn.example"), ("warn", _PROG["host_deferred"].format(host="cdn.example"))),
            (events.HostLastResort(host="cdn.example"), ("warn", _PROG["host_last_resort"].format(host="cdn.example"))),
            (events.HostDropped(host="cdn.example"), ("warn", _PROG["host_dropped"].format(host="cdn.example"))),
            (
                events.ExtractorFallback(kind="extractor_fallback", host="dood.example", redacted_text="HTTP 403"),
                ("warn", _PROG["extractor_fallback"].format(host="dood.example", exc="HTTP 403")),
            ),
            (
                events.ExtractorFallback(kind="jwplayer_fallback", host="new.example", redacted_text="no sources"),
                ("warn", _PROG["jwplayer_fallback"].format(host="new.example", exc="no sources")),
            ),
            (
                events.DownloadStarted(title="Show", episode_label="5.5", directory="/videos"),
                ("info", _DL["starting"].format(title="Show", ep_label="5.5", dir="/videos")),
            ),
            (
                events.LegacyPartMoved(path="/videos/Show - ep1.mp4.part.legacy-ffmpeg"),
                ("warn", _DL["legacy_part_moved"].format(path="/videos/Show - ep1.mp4.part.legacy-ffmpeg")),
            ),
            (
                events.DownloadSaved(path="/videos/Show - ep1.mp4"),
                ("success", _DL["saved"].format(path="/videos/Show - ep1.mp4")),
            ),
        ],
        ids=lambda value: type(value).__name__ if isinstance(value, events.EngineEvent) else "",
    )
    def test_event_renders_as_one_call(self, console, capsys, event, call):
        ConsoleEvents().emit(event)
        assert console.calls == [call]
        assert capsys.readouterr().out == ""

    def test_xhr_fallback_formats_whole_episode_numbers_without_a_fraction(self, console):
        ConsoleEvents().emit(events.XhrFallback(episode=5.0, reason="missing_token"))
        assert "5 " in console.calls[0][1]
        assert "5.0" not in console.calls[0][1]
        assert "missing_token" in console.calls[0][1]

    def test_failed_source_names_player_episode_and_reason(self, console):
        ConsoleEvents().emit(events.SourceAttemptFailed(player="CDA", episode=5.5, redacted_text="no stream"))
        message = console.calls[0][1]
        assert "'CDA'" in message
        assert "5.5" in message
        assert "no stream" in message

    def test_download_start_names_title_episode_and_directory(self, console):
        ConsoleEvents().emit(events.DownloadStarted(title="Show", episode_label="12", directory="/videos"))
        message = console.calls[0][1]
        assert "Show" in message
        assert "ep12" in message
        assert "/videos" in message


@pytest.mark.unit
class TestDownloadLog:
    def test_line_is_printed_plain_and_not_routed_through_rich(self, console, capsys):
        with _terminal(200):
            ConsoleEvents().emit(events.DownloadLog(redacted_line="[download] 100% of [bold]5.00MiB[/bold]"))
        assert capsys.readouterr().out == "[download] 100% of [bold]5.00MiB[/bold]\n"
        assert console.calls == []

    def test_line_longer_than_the_terminal_is_truncated_with_an_ellipsis(self, console, capsys):
        with _terminal(20):
            ConsoleEvents().emit(events.DownloadLog(redacted_line="x" * 50))
        assert capsys.readouterr().out == "x" * 19 + "…\n"

    def test_line_exactly_as_wide_as_the_terminal_is_kept(self, console, capsys):
        with _terminal(20):
            ConsoleEvents().emit(events.DownloadLog(redacted_line="x" * 20))
        assert capsys.readouterr().out == "x" * 20 + "\n"

    def test_local_paths_untouched(self, console, capsys):
        with _terminal(200):
            ConsoleEvents().emit(
                events.DownloadLog(redacted_line=r"[download] Destination: C:\Users\Test\Videos\Show - ep4.mp4")
            )
        assert r"C:\Users\Test\Videos\Show - ep4.mp4" in capsys.readouterr().out

    def test_engine_redacts_before_the_renderer_truncates(self, console, capsys):
        head = "x" * 40 + " https://h.example/m.m3u8?token="
        # Truncating first would keep "token=SECRET_…" — and a cut value no longer looks like a full query.
        with _terminal(len(head) + 8):
            download._EventLogger(ConsoleEvents()).info(head + "SECRET_TAIL_VALUE_THAT_IS_LONG")
        out = capsys.readouterr().out
        assert "SECRET" not in out
        assert out.rstrip("\n") == head + "<redact…"

    @pytest.mark.parametrize(("method", "prefix"), [("warning", "[warn] "), ("error", "[error] ")])
    def test_ytdlp_warning_and_error_reach_the_terminal_redacted(self, console, capsys, method, prefix):
        with _terminal(200):
            getattr(download._EventLogger(ConsoleEvents()), method)("fragment failed: master.m3u8?token=SECRET&expires=123")
        out = capsys.readouterr().out
        assert out.startswith(prefix)
        assert "SECRET" not in out and "123" not in out
        assert "master.m3u8?token=<redacted>&expires=<redacted>" in out

    def test_suppressed_ytdlp_chatter_prints_nothing(self, console, capsys):
        download._EventLogger(ConsoleEvents()).info("[generic] Extracting URL: https://h/m?token=SECRET")
        assert capsys.readouterr().out == ""


@dataclass(frozen=True)
class _FutureEvent(events.EngineEvent):
    detail: str = "added by a newer engine"


@pytest.mark.unit
class TestIgnoredEvents:
    @pytest.mark.parametrize("event", [_FutureEvent(), events.EngineEvent(), events.ClearanceUnavailable()])
    def test_event_without_a_rendering_is_ignored(self, console, capsys, event):
        ConsoleEvents().emit(event)
        assert console.calls == []
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    def test_unknown_event_does_not_disturb_an_open_spinner(self, console):
        renderer = ConsoleEvents()
        renderer.emit(events.PhaseStarted("antibot_delay"))
        renderer.emit(_FutureEvent())
        renderer.emit(events.PhaseFinished("antibot_delay"))
        assert [name for name, _ in console.calls] == ["spinner_enter", "spinner_exit"]


@pytest.mark.unit
class TestCoverageOfEventTypes:
    def test_every_engine_event_type_is_exercised_here(self):
        declared = {
            cls
            for cls in vars(events).values()
            if isinstance(cls, type) and issubclass(cls, events.EngineEvent) and cls is not events.EngineEvent
        }
        assert declared == {
            events.ClearanceRequested,
            events.ClearanceUnavailable,
            events.PhaseStarted,
            events.PhaseFinished,
            events.XhrFallback,
            events.EmbedResolved,
            events.SourceAttemptFailed,
            events.HostDeferred,
            events.HostLastResort,
            events.HostDropped,
            events.ExtractorFallback,
            events.DownloadStarted,
            events.DownloadLog,
            events.LegacyPartMoved,
            events.DownloadSaved,
        }
