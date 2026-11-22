import pytest

from alt_ani_core.models import PlayerEntry, Stream, StreamProtocol, StreamVariant
from alt_ani_core.rules.criteria import (
    NoMatch,
    criteria_for,
    match_source,
    match_variant,
    resume_candidate,
    same_player,
    same_resource,
    source_identity,
    variant_identity,
)
from alt_ani_core.state import (
    DownloadCriteria,
    PartialTransfer,
    Preferences,
    QualityCriterion,
    ResourceValidator,
    StorageLocator,
)


def _player(audio: str = "jp", subs: str = "pl", **fields) -> PlayerEntry:
    return PlayerEntry(online_id=fields.pop("online_id", "1"), player="Host", lang_audio=audio, lang_subs=subs, **fields)


def _variant(label: str) -> StreamVariant:
    return StreamVariant(url=f"https://cdn.example/{label or 'x'}.m3u8", protocol=StreamProtocol.HLS, label=label)


def _stream(*labels: str, default_index: int = 0) -> Stream:
    return Stream(variants=tuple(_variant(label) for label in labels), default_index=default_index)


@pytest.mark.unit
class TestCriteriaValues:
    def test_defaults_are_any_language_and_best_quality(self):
        criteria = DownloadCriteria()
        assert (criteria.audio, criteria.subs, criteria.quality.max_height) == ("any", "any", None)

    def test_languages_are_normalized(self):
        assert DownloadCriteria(audio=" PL ", subs="EN").audio == "pl"

    def test_audio_cannot_require_absence(self):
        with pytest.raises(ValueError):
            DownloadCriteria(audio="none")

    def test_empty_language_is_rejected(self):
        with pytest.raises(ValueError):
            DownloadCriteria(subs=" ")

    @pytest.mark.parametrize("text,height", [("best", None), ("max:720", 720), ("max:2160", 2160)])
    def test_quality_round_trips(self, text, height):
        quality = QualityCriterion.parse(text)
        assert quality.max_height == height
        assert quality.encode() == text

    @pytest.mark.parametrize("text", ["720p", "max:0", "max:-1", "worst", "max:"])
    def test_unknown_quality_text_is_rejected(self, text):
        with pytest.raises(ValueError):
            QualityCriterion.parse(text)

    def test_one_off_change_overrides_only_the_given_field(self):
        prefs = Preferences(audio="jp", subs="pl")
        criteria = criteria_for(prefs, quality=QualityCriterion(480))
        assert criteria == DownloadCriteria(audio="jp", subs="pl", quality=QualityCriterion(480))


@pytest.mark.unit
class TestMatchSource:
    @pytest.mark.parametrize(
        "audio,subs,wanted_audio,wanted_subs,expected",
        [
            ("jp", "pl", "any", "any", True),
            ("", "", "any", "any", True),
            ("jp", "pl", "jp", "any", True),
            ("PL", "", "pl", "any", True),
            ("pl", "", "jp", "any", False),
            ("", "pl", "jp", "any", False),
            ("jp", "none", "any", "none", True),
            ("jp", "", "any", "none", False),
            ("jp", "pl", "any", "none", False),
            ("jp", "pl", "any", "pl", True),
            ("jp", "en", "any", "pl", False),
            ("jp", "", "any", "pl", False),
        ],
    )
    def test_matrix(self, audio, subs, wanted_audio, wanted_subs, expected):
        criteria = DownloadCriteria(audio=wanted_audio, subs=wanted_subs)
        assert match_source(_player(audio, subs), criteria) is expected


@pytest.mark.unit
class TestMatchVariant:
    def test_best_takes_the_highest_known_height(self):
        assert match_variant(_stream("480p", "1080p", "720p"), QualityCriterion()).label == "1080p"

    def test_best_without_any_known_height_takes_the_extractor_default(self):
        stream = _stream("", "hd", default_index=1)
        assert match_variant(stream, QualityCriterion()) is stream.default

    def test_best_ignores_unknown_heights_when_some_are_known(self):
        assert match_variant(_stream("hd", "480p"), QualityCriterion()).label == "480p"

    def test_limit_takes_the_best_variant_within_it(self):
        assert match_variant(_stream("1080p", "480p"), QualityCriterion(720)).label == "480p"

    def test_limit_accepts_an_exact_height(self):
        assert match_variant(_stream("1080p", "720p", "480p"), QualityCriterion(720)).label == "720p"

    def test_only_higher_variants_is_no_match(self):
        assert match_variant(_stream("1080p"), QualityCriterion(720)) == NoMatch("quality")

    def test_unknown_height_never_satisfies_a_limit(self):
        assert match_variant(_stream("", "hd"), QualityCriterion(720)) == NoMatch("quality")

    def test_no_match_is_not_weakened_to_the_default(self):
        stream = _stream("1080p", "", default_index=1)
        assert isinstance(match_variant(stream, QualityCriterion(360)), NoMatch)


@pytest.mark.unit
class TestIdentities:
    def test_source_identity_keeps_no_url(self):
        entry = _player(source="https://subs.example/group?token=secret", subs_author="  Grupa  ")
        identity = source_identity(entry)
        assert identity.source == "subs.example"
        assert identity.subs_author == "grupa"
        assert "secret" not in repr(identity)

    def test_same_player_ignores_the_episode_online_id(self):
        assert same_player(source_identity(_player(online_id="1")), source_identity(_player(online_id="2")))

    def test_variant_identity_has_no_url_and_unknown_height_is_none(self):
        identity = variant_identity(_variant("hd"))
        assert (identity.label, identity.height, identity.protocol) == ("hd", None, "hls")


def _partial(validator: ResourceValidator | None, label: str = "720p") -> PartialTransfer:
    return PartialTransfer(
        locator=StorageLocator("fs", "/tmp/x.part"),
        source=source_identity(_player()),
        variant=variant_identity(_variant(label)),
        validator=validator,
    )


_VALIDATOR = ResourceValidator("progressive", (("content_length", 1000), ("etag_sha256", "ab")))


@pytest.mark.unit
class TestResume:
    def test_same_source_and_variant_is_only_a_candidate(self):
        partial = _partial(_VALIDATOR)
        assert resume_candidate(partial, partial.source, partial.variant)

    def test_no_validator_is_not_a_candidate(self):
        partial = _partial(None)
        assert not resume_candidate(partial, partial.source, partial.variant)

    def test_other_variant_is_not_a_candidate(self):
        partial = _partial(_VALIDATOR)
        assert not resume_candidate(partial, partial.source, variant_identity(_variant("1080p")))

    def test_no_partial_is_not_a_candidate(self):
        partial = _partial(_VALIDATOR)
        assert not resume_candidate(None, partial.source, partial.variant)

    def test_same_resource_needs_equal_evidence(self):
        observed = ResourceValidator("progressive", (("etag_sha256", "ab"), ("content_length", 1000)))
        assert same_resource(_VALIDATOR, observed)

    @pytest.mark.parametrize(
        "observed",
        [
            None,
            ResourceValidator("progressive", (("content_length", 1000),)),
            ResourceValidator("progressive", (("content_length", 1001), ("etag_sha256", "ab"))),
            ResourceValidator("hls", (("content_length", 1000), ("etag_sha256", "ab"))),
        ],
        ids=["unverifiable", "missing-field", "changed-length", "other-kind"],
    )
    def test_anything_less_than_equal_evidence_restarts(self, observed):
        assert not same_resource(_VALIDATOR, observed)

    def test_empty_evidence_is_never_the_same_resource(self):
        empty = ResourceValidator("progressive", ())
        assert not same_resource(empty, empty)
