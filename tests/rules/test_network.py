import pytest

from alt_ani_core.rules.network import CellularConsent, download_allowed, stream_allowed
from alt_ani_core.state import DownloadNetwork, EpisodeKey, NetworkKind

EP5 = EpisodeKey("1", 5)
EP6 = EpisodeKey("1", 6)


@pytest.mark.unit
class TestDownloadAllowed:
    @pytest.mark.parametrize(
        "network,policy,job_consent,expected",
        [
            (NetworkKind.WIFI, DownloadNetwork.WIFI_ONLY, False, True),
            (NetworkKind.CELLULAR, DownloadNetwork.WIFI_ONLY, False, False),
            (NetworkKind.CELLULAR, DownloadNetwork.WIFI_ONLY, True, True),
            (NetworkKind.CELLULAR, DownloadNetwork.ANY, False, True),
            (NetworkKind.OFFLINE, DownloadNetwork.ANY, True, False),
        ],
    )
    def test_matrix(self, network, policy, job_consent, expected):
        assert download_allowed(network, policy, job_consent) is expected


@pytest.mark.unit
class TestStreamAllowed:
    def test_wifi_needs_no_consent(self):
        assert stream_allowed(NetworkKind.WIFI, None, EP5)

    def test_cellular_needs_consent(self):
        assert not stream_allowed(NetworkKind.CELLULAR, None, EP5)

    def test_consent_covers_its_episode(self):
        assert stream_allowed(NetworkKind.CELLULAR, CellularConsent(EP5), EP5)

    def test_consent_does_not_carry_to_the_next_episode(self):
        assert not stream_allowed(NetworkKind.CELLULAR, CellularConsent(EP5), EP6)

    def test_offline_never_streams(self):
        assert not stream_allowed(NetworkKind.OFFLINE, CellularConsent(EP5), EP5)
