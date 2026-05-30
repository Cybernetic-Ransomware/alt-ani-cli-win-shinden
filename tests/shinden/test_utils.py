"""Tests for shinden/utils.py — pure functions, no fixtures needed."""

import pytest

from alt_ani_cli.errors import ShindenError
from alt_ani_cli.shinden.utils import _AGE_GATE_HINTS, _check_age_gate, _normalize_title


@pytest.mark.unit
class TestNormalizeTitle:
    def test_adds_space_after_colon_before_letter(self):
        assert _normalize_title("Blue Archive:Beautiful Day") == "Blue Archive: Beautiful Day"

    def test_adds_space_after_interpunct_before_letter(self):
        assert _normalize_title("Sword Art Online·Alicization") == "Sword Art Online· Alicization"

    def test_splits_camel_case(self):
        assert _normalize_title("BlueArchive") == "Blue Archive"

    def test_strips_whitespace(self):
        assert _normalize_title("  Shingeki  ") == "Shingeki"

    def test_unchanged_when_no_patterns(self):
        assert _normalize_title("One Piece") == "One Piece"

    def test_empty_string(self):
        assert _normalize_title("") == ""

    def test_combined_colon_and_camel(self):
        # Realistic: selectolax concatenates title+subtitle without space
        result = _normalize_title("Blue Archive:BeautifulDay")
        assert "Blue Archive:" in result
        assert "Beautiful" in result


@pytest.mark.unit
class TestCheckAgeGate:
    @pytest.mark.parametrize("hint", _AGE_GATE_HINTS)
    def test_raises_on_each_known_hint(self, hint):
        with pytest.raises(ShindenError):
            _check_age_gate(f"<html>Aby kontynuować {hint}.</html>")

    def test_case_insensitive_match(self):
        with pytest.raises(ShindenError):
            _check_age_gate("MUSISZ MIEĆ UKOŃCZONE 18 LAT")

    def test_clean_page_does_not_raise(self):
        _check_age_gate("<html><body><h1>Naruto</h1></body></html>")

    def test_hints_contain_expected_polish_tokens(self):
        """Anchor: parsing tokens must stay in Polish to match shinden.pl HTML.

        _AGE_GATE_HINTS are domain data (HTML fingerprints), not UI strings.
        If someone translates them to English, shinden.pl age-gated pages will
        pass through silently. This test breaks intentionally in that case.
        """
        assert "musisz mieć ukończone 18" in _AGE_GATE_HINTS
        assert "treści dla dorosłych" in _AGE_GATE_HINTS
        assert "potwierdź wiek" in _AGE_GATE_HINTS
