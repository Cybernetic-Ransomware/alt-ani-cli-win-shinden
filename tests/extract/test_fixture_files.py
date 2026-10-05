"""Invariants for the host response fixtures in tests/fixtures/extract/."""

import json
import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "extract"

# Scheme-ful URLs (JSON-escaped slashes included) and protocol-relative string literals.
_URL_HOST_RE = re.compile(r"https?:\\?/\\?/([^/\\\"'\s:?]+)|[\"']//([^/\"'\s:?]+)")


def _fixture_files():
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_file())


@pytest.mark.unit
class TestExtractFixtureFiles:
    def test_directory_exists_and_is_not_empty(self):
        assert FIXTURES_DIR.is_dir()
        assert _fixture_files()

    @pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
    def test_fixture_is_utf8_and_json_fixtures_parse(self, path):
        text = path.read_bytes().decode("utf-8")
        if path.suffix == ".json":
            json.loads(text)

    @pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
    def test_fixture_urls_point_only_at_reserved_example_hosts(self, path):
        hosts = {a or b for a, b in _URL_HOST_RE.findall(path.read_text(encoding="utf-8"))}
        assert all(h.endswith(".example") for h in hosts), hosts
