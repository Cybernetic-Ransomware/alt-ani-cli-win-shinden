from pathlib import Path
from unittest.mock import MagicMock

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "shinden"


@pytest.fixture
def shinden_html():
    """Sanitised Shinden 2.5 responses from tests/fixtures/shinden/."""

    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def diag_events(monkeypatch):
    """Record diagnostics events as (event, fields) instead of writing the log."""
    from alt_ani_cli import diagnostics

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(diagnostics, "_emit", lambda event, **fields: events.append((event, fields)))
    return events


@pytest.fixture
def fake_response():
    """Build a curl_cffi-like response; status >= 400 makes raise_for_status() raise."""

    def _make(text: str = "", status: int = 200) -> MagicMock:
        resp = MagicMock(status_code=status, text=text)
        if status >= 400:
            resp.raise_for_status.side_effect = CurlHTTPError(f"HTTP {status}")
        else:
            resp.raise_for_status.return_value = None
        return resp

    return _make
