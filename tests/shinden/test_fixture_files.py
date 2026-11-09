"""Invariants for the sanitised Shinden 2.5 responses in tests/fixtures/shinden/."""

import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "shinden"
_URL_HOST_RE = re.compile(r"https?://([^/\"'\s:?<>]+)|[\"']//([^/\"'\s:?<>]+)")
# Session-bound or account-bound values that must never be committed.
_FORBIDDEN = (
    "data-player-auth",
    "data-turnstile-key",
    "data-sitekey",
    "data-token-url",
    "data-login-",
    "cf_clearance",
    "/api/user/jwt",
    "X2d1ZXN0",  # base64 of "_guest_" — the player auth token prefix
)


def _fixture_files():
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_file())


@pytest.mark.unit
class TestShindenFixtureFiles:
    def test_directory_exists_and_is_not_empty(self):
        assert FIXTURES_DIR.is_dir()
        assert _fixture_files()

    @pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
    def test_fixture_is_utf8(self, path):
        path.read_bytes().decode("utf-8")

    @pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
    def test_no_tokens_or_session_data(self, path):
        text = path.read_text(encoding="utf-8")
        assert [marker for marker in _FORBIDDEN if marker in text] == []

    @pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
    def test_absolute_urls_point_only_at_shinden_or_example_hosts(self, path):
        hosts = {a or b for a, b in _URL_HOST_RE.findall(path.read_text(encoding="utf-8"))}
        assert all(h == "shinden.pl" or h.endswith(".example") for h in hosts), hosts
