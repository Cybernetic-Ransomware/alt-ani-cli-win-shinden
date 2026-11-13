"""Playback preparation: explicit criteria, no fallback to the default variant, a refresh keeps what was chosen."""

import pytest

from alt_ani_core.errors import CriteriaMismatchError, InteractionRequiredError, PlaybackBlockedError, SourcesExhaustedError
from alt_ani_core.history.local import open_local_state
from alt_ani_core.models import EmbedURL, PlayerEntry, ResolvedStream, SourceAttempt, Stream, StreamProtocol, StreamVariant
from alt_ani_core.rules.criteria import variant_identity
from alt_ani_core.rules.network import CellularConsent
from alt_ani_core.services.playback import PlaybackSelection, PlaybackService, check_stream
from alt_ani_core.state import DownloadCriteria, EpisodeKey, NetworkKind, QualityCriterion, VariantIdentity

JP = PlayerEntry(online_id="1", player="host", lang_audio="jp", lang_subs="pl")
PL = PlayerEntry(online_id="2", player="other", lang_audio="pl", lang_subs="")
KEY = EpisodeKey("7", 1)


def stream(*labels: str, default: int = 0) -> Stream:
    variants = tuple(StreamVariant(url=f"https://cdn/{lab}.m3u8", protocol=StreamProtocol.HLS, label=lab) for lab in labels)
    return Stream(variants=variants, default_index=default)


class FakeSession:
    def __init__(self, entries, streams, error: Exception | None = None) -> None:
        self.sources = tuple(entries)
        self.streams = streams
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    def rank(self, players):
        return list(players)

    def resolve(self, chosen, *, candidates=(), policy=None, force_fresh=False, cancel=None):
        self.calls.append((chosen.online_id, force_fresh))
        if self.error is not None:
            raise self.error
        return ResolvedStream(self.streams[chosen.online_id].pop(0), chosen, EmbedURL("https://host/e", "https://shinden.pl"))


@pytest.fixture
def service(tmp_path):
    return PlaybackService(open_local_state(tmp_path / "h.db"), None)


@pytest.mark.unit
class TestOnline:
    def test_best_known_height_by_default(self, service):
        result = service.online(FakeSession([JP], {"1": [stream("480p", "1080p")]}), DownloadCriteria())
        assert result.variant.label == "1080p"
        assert result.selection == PlaybackSelection("1", variant_identity(result.variant), DownloadCriteria())
        assert result.matches_criteria

    def test_language_is_never_widened(self, service):
        with pytest.raises(CriteriaMismatchError) as err:
            service.online(FakeSession([JP], {}), DownloadCriteria(audio="pl", subs="pl"))
        assert err.value.params == {"reason": "audio"}
        with pytest.raises(CriteriaMismatchError) as err:
            service.online(FakeSession([JP], {}), DownloadCriteria(audio="jp", subs="en"))
        assert err.value.params == {"reason": "subs"}

    def test_quality_limit_has_no_fallback_to_the_default(self, service):
        session = FakeSession([JP], {"1": [stream("1080p", "720p", default=0)]})
        with pytest.raises(CriteriaMismatchError) as err:
            service.online(session, DownloadCriteria(quality=QualityCriterion(480)))
        assert err.value.params == {"reason": "quality"}

    def test_explicit_source_is_played_and_flagged(self, service):
        result = service.online(FakeSession([JP, PL], {"2": [stream("720p")]}), DownloadCriteria(audio="jp"), online_id="2")
        assert result.source == PL and result.matches_criteria is False

    def test_host_needing_interaction(self, service):
        error = SourcesExhaustedError("x", attempts=(SourceAttempt(JP, "host.example", "JavaScriptRequiredError"),))
        with pytest.raises(InteractionRequiredError) as err:
            service.online(FakeSession([JP], {}, error), DownloadCriteria())
        assert err.value.params == {"scope": "host", "host": "host.example", "online_id": "1"}


@pytest.mark.unit
class TestRefresh:
    def selection(self, label: str, criteria: DownloadCriteria | None = None) -> PlaybackSelection:
        identity = VariantIdentity(label=label, height=int(label[:-1]), protocol="hls")
        return PlaybackSelection("1", identity, criteria or DownloadCriteria())

    def test_keeps_the_same_variant_even_if_not_the_best(self, service):
        session = FakeSession([JP], {"1": [stream("480p", "1080p")]})
        result = service.refresh(session, self.selection("480p"))
        assert result.variant.label == "480p" and result.variant_changed is False
        assert session.calls == [("1", True)]

    def test_missing_variant_falls_back_to_the_kept_criteria_only(self, service):
        limited = self.selection("480p", DownloadCriteria(quality=QualityCriterion(720)))
        result = service.refresh(FakeSession([JP], {"1": [stream("720p", "1080p")]}), limited)
        assert result.variant.label == "720p" and result.variant_changed is True
        assert result.selection.criteria == limited.criteria
        with pytest.raises(CriteriaMismatchError):
            service.refresh(FakeSession([JP], {"1": [stream("1080p")]}), limited)

    def test_explicit_variant_switch(self, service):
        wanted = VariantIdentity(label="1080p", height=1080, protocol="hls")
        result = service.refresh(FakeSession([JP], {"1": [stream("480p", "1080p")]}), self.selection("480p"), variant=wanted)
        assert result.selection.variant == wanted


@pytest.mark.unit
class TestStreamGate:
    def test_wifi(self):
        check_stream(NetworkKind.WIFI, None, KEY)

    def test_offline(self):
        with pytest.raises(PlaybackBlockedError) as err:
            check_stream(NetworkKind.OFFLINE, CellularConsent(KEY), KEY)
        assert err.value.code == "network.offline"

    def test_cellular_needs_consent_for_this_episode(self):
        with pytest.raises(PlaybackBlockedError) as err:
            check_stream(NetworkKind.CELLULAR, CellularConsent(EpisodeKey("7", 2)), KEY)
        assert err.value.code == "network.cellular_consent_required"
        check_stream(NetworkKind.CELLULAR, CellularConsent(KEY), KEY)
