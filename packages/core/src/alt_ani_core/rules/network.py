"""Which transfers the current network allows; the platform classifies the network, the engine never detects it."""

from dataclasses import dataclass

from alt_ani_core.state import DownloadNetwork, EpisodeKey, NetworkKind


def download_allowed(network: NetworkKind, policy: DownloadNetwork, job_cellular_allowed: bool) -> bool:
    """The global policy is read live, the per-job consent belongs to that job only."""
    if network is NetworkKind.WIFI:
        return True
    if network is NetworkKind.CELLULAR:
        return job_cellular_allowed or policy is DownloadNetwork.ANY
    return False


@dataclass(frozen=True)
class CellularConsent:
    """Consent to stream one episode over mobile data; held by the playback session, never persisted."""

    episode: EpisodeKey


def stream_allowed(network: NetworkKind, consent: CellularConsent | None, episode: EpisodeKey) -> bool:
    """New transfers only; an already buffered part plays without consent."""
    if network is NetworkKind.WIFI:
        return True
    if network is NetworkKind.CELLULAR:
        return consent is not None and consent.episode == episode
    return False
