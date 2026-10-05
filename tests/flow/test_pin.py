"""Tests for the download source pin fingerprint (flow/pin.py)."""

import dataclasses

import pytest

from alt_ani_cli.flow.pin import PlayerFingerprint, find_pinned, player_fingerprint, source_identity
from alt_ani_cli.models import PlayerEntry

_BASE = PlayerEntry(
    online_id="111",
    player="CDA",
    lang_audio="jp",
    lang_subs="pl",
    max_res="1080p",
    date_added="2024-01-01",
    subs_author="Mioro-Subs",
    source="https://www.miorosubs.com/?ep=4",
)


@pytest.mark.unit
class TestPlayerFingerprint:
    def test_ignores_online_id_and_date_added(self):
        other = dataclasses.replace(_BASE, online_id="222", date_added="2024-02-02")
        assert player_fingerprint(other) == player_fingerprint(_BASE)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("player", "Sibnet"),
            ("lang_audio", "pl"),
            ("lang_subs", "en"),
            ("max_res", "720p"),
            ("subs_author", "Other-Subs"),
        ],
    )
    def test_identity_field_change_gives_different_fingerprint(self, field, value):
        other = dataclasses.replace(_BASE, **{field: value})
        assert player_fingerprint(other) != player_fingerprint(_BASE)

    def test_different_source_hosts_give_different_fingerprint(self):
        other = dataclasses.replace(_BASE, source="https://anisubs.pl/?ep=4")
        assert player_fingerprint(other) != player_fingerprint(_BASE)

    def test_source_url_episode_query_does_not_split_identity(self):
        other = dataclasses.replace(_BASE, online_id="222", source="http://miorosubs.com/?ep=5")
        assert player_fingerprint(other) == player_fingerprint(_BASE)

    def test_text_fields_normalize_whitespace_and_case(self):
        other = dataclasses.replace(_BASE, player="  cda ", subs_author="mioro-subs", max_res="1080P")
        assert player_fingerprint(other) == player_fingerprint(_BASE)

    def test_none_and_empty_optional_fields_are_equal(self):
        a = PlayerEntry(online_id="1", player="CDA", lang_audio="jp", lang_subs="")
        b = PlayerEntry(online_id="2", player="CDA", lang_audio="jp", lang_subs="", max_res="", subs_author=" ", source="")
        assert player_fingerprint(a) == player_fingerprint(b)

    def test_is_hashable_value(self):
        fp = player_fingerprint(_BASE)
        assert isinstance(fp, PlayerFingerprint)
        assert {fp} == {player_fingerprint(_BASE)}


@pytest.mark.unit
class TestSourceIdentity:
    def test_url_variants_share_host_identity(self):
        assert source_identity("https://www.miorosubs.com/?ep=4") == "miorosubs.com"
        assert source_identity("http://miorosubs.com/?ep=5") == "miorosubs.com"

    def test_hostname_is_casefolded_without_www(self):
        assert source_identity("https://www.MioroSubs.com/?episode=4#top") == "miorosubs.com"

    def test_non_url_text_collapses_whitespace_and_case(self):
        assert source_identity("  Grupa   Mioro\tSubs ") == "grupa mioro subs"

    def test_none_and_blank_are_empty(self):
        assert source_identity(None) == ""
        assert source_identity("   ") == ""

    def test_malformed_url_falls_back_to_text(self):
        assert source_identity("http://[broken") == "http://[broken"


@pytest.mark.unit
class TestFindPinned:
    def test_returns_first_match_in_given_order(self):
        a = dataclasses.replace(_BASE, online_id="a")
        b = dataclasses.replace(_BASE, online_id="b")
        other = dataclasses.replace(_BASE, online_id="c", player="Sibnet")
        assert find_pinned([other, b, a], player_fingerprint(_BASE)) is b

    def test_no_match_returns_none(self):
        other = dataclasses.replace(_BASE, player="Sibnet")
        assert find_pinned([other], player_fingerprint(_BASE)) is None
