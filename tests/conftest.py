import logging

import pytest


@pytest.fixture(autouse=True)
def _isolate_diagnostics_dir(tmp_path, monkeypatch):
    """Every test gets its own DIAG_DIR — main() now configures diagnostics in noninteractive runs too."""
    from alt_ani_cli import diagnostics

    monkeypatch.setattr(diagnostics, "DIAG_DIR", tmp_path / "diagnostics")
    yield
    for handler in [h for h in diagnostics._logger.handlers if not isinstance(h, logging.NullHandler)]:
        diagnostics._logger.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _isolate_history_file(tmp_path, monkeypatch):
    """Successful downloads write history — never touch the real history.json."""
    from alt_ani_cli import history

    monkeypatch.setattr(history, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "state" / "history.json")


@pytest.fixture
def player_show_html():
    return '<html><body><iframe src="//video.sibnet.ru/shell.php?videoid=1234567"></iframe></body></html>'
