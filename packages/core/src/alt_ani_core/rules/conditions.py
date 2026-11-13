"""Conditions the scheduler holds jobs for; interaction and source decisions are never inferred from them."""

from dataclasses import dataclass

from alt_ani_core.rules.network import download_allowed
from alt_ani_core.state import DownloadJob, DownloadNetwork, NetworkKind, WaitReason

CONDITION_REASONS = frozenset({WaitReason.NETWORK, WaitReason.STORAGE})
# kept free beyond the bytes a job still needs, so a download does not fill the disk to the last block
STORAGE_RESERVE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Conditions:
    network: NetworkKind
    policy: DownloadNetwork
    free_bytes: int | None
    storage_notified: bool = False


def storage_ok(job: DownloadJob, conditions: Conditions) -> bool:
    """Released only on evidence: enough free space for the known remainder, or the platform saying space was freed."""
    if conditions.storage_notified:
        return True
    if conditions.free_bytes is None or job.bytes_total is None:
        return False
    remaining = max(job.bytes_total - (job.bytes_done or 0), 0)
    return conditions.free_bytes >= remaining + STORAGE_RESERVE_BYTES


def blocking_condition(job: DownloadJob, conditions: Conditions, *, storage_blocked: bool) -> WaitReason | None:
    """Network first, then storage; ``storage_blocked`` means some job hit a full disk and no evidence lifted it."""
    if not download_allowed(conditions.network, conditions.policy, job.cellular_allowed):
        return WaitReason.NETWORK
    if storage_blocked and not storage_ok(job, conditions):
        return WaitReason.STORAGE
    return None


def flaresolverr_allowed(configured: bool, network: NetworkKind) -> bool:
    """Only when the configuration allows it and the device is on Wi-Fi; mobile data never uses the solver."""
    return configured and network is NetworkKind.WIFI
