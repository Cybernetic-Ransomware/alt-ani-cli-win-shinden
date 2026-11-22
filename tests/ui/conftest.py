import io
from dataclasses import dataclass

import pytest
from rich.console import Console

from alt_ani_cli.ui import progress


@dataclass
class RichOutput:
    out: io.StringIO
    err: io.StringIO
    out_console: Console
    err_console: Console

    @property
    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()


def _install(monkeypatch, **console_kw) -> RichOutput:
    out, err = io.StringIO(), io.StringIO()
    out_console = Console(file=out, width=300, legacy_windows=False, highlight=False, **console_kw)
    err_console = Console(file=err, width=300, legacy_windows=False, highlight=False, **console_kw)
    monkeypatch.setattr(progress, "_console", out_console)
    monkeypatch.setattr(progress, "_err_console", err_console)
    return RichOutput(out, err, out_console, err_console)


@pytest.fixture
def rich_plain(monkeypatch) -> RichOutput:
    """Real Rich rendering of ui.progress into buffers, without colors."""
    return _install(monkeypatch, color_system=None)


@pytest.fixture
def rich_ansi(monkeypatch) -> RichOutput:
    """Real Rich rendering of ui.progress into buffers, with ANSI styles kept."""
    return _install(monkeypatch, force_terminal=True, color_system="truecolor")
