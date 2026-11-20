"""Fakes for executor tests: sources served by a local server, a controllable transfer and quick timings."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from alt_ani_core.history.local import LocalState, open_local_state
from alt_ani_core.models import EpisodeRow, PlayerEntry, SeriesRef, Stream, StreamProtocol, StreamVariant
from alt_ani_core.services.offline import OfflineQueue
from alt_ani_core.services.offline.attempt import ExecutorConfig
from alt_ani_core.services.offline.network import StaticNetwork
from alt_ani_core.state import NetworkKind, ResourceValidator
from alt_ani_core.storage.fs import FsFileStore
from alt_ani_core.transfer import CheckpointPolicy, TransferResult

SERIES = SeriesRef(id="7", slug="fate", title="Fate Zero", url="https://shinden.pl/series/7-fate")


def episode(number: float) -> EpisodeRow:
    return EpisodeRow(number=number, title=f"Episode {number:g}", url=f"https://shinden.pl/episode/7-fate/view/{number:g}")


def player(online_id: str, *, audio: str = "jp", subs: str = "pl", name: str = "host") -> PlayerEntry:
    return PlayerEntry(online_id=online_id, player=name, lang_audio=audio, lang_subs=subs, max_res="1080p")


def progressive(url: str, label: str = "720p") -> Stream:
    return Stream(variants=(StreamVariant(url=url, protocol=StreamProtocol.PROGRESSIVE, label=label, container="mp4"),))


def fast_config(**changes) -> ExecutorConfig:
    config = ExecutorConfig(
        progress_interval_sec=0.0,
        checkpoint=CheckpointPolicy(interval_sec=0.05, every_bytes=64 * 1024),
        socket_timeout_sec=3.0,
        monitor_interval_sec=0.02,
        idle_interval_sec=0.2,
        unreadable_stop_sec=0.5,
        store_retries=2,
        store_retry_delay_sec=0.01,
    )
    for key, value in changes.items():
        setattr(config, key, value)
    return config


@dataclass
class FakeEpisode:
    entries: list[PlayerEntry]
    streams: dict[str, Callable[[bool], Stream]]
    resolved: list[tuple[str, bool]] = field(default_factory=list)

    def resolve(self, entry: PlayerEntry, *, fresh: bool, cancel) -> Stream:
        cancel.raise_if_cancelled()
        self.resolved.append((entry.online_id, fresh))
        return self.streams[entry.online_id](fresh)


class FakeSources:
    """Same players for every episode; ``open_error`` simulates Shinden itself failing or needing interaction."""

    def __init__(self, entries: list[PlayerEntry], streams: dict[str, Callable[[bool], Stream]]) -> None:
        self.episode = FakeEpisode(entries, streams)
        self.networks: list[NetworkKind] = []
        self.open_error: Exception | None = None
        self.opened = 0

    def open(self, episode: EpisodeRow, *, grant, cancel) -> FakeEpisode:
        cancel.raise_if_cancelled()
        self.opened += 1
        self.last_grant = grant
        if self.open_error is not None:
            raise self.open_error
        return self.episode

    def network_changed(self, network: NetworkKind) -> None:
        self.networks.append(network)


class GatedTransfer:
    """A transfer that writes nothing until released; its I/O "ends" only after ``io_done`` is set once cancelled."""

    def __init__(self) -> None:
        self.started = threading.Semaphore(0)
        self.release = threading.Event()
        self.io_done = threading.Event()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.lock = threading.Lock()
        self.result_bytes = b"video-bytes"
        self.fail_with: BaseException | None = None
        self.resumed: list[bool] = []

    def __call__(self, variant, work: Path, *, resume, callbacks, policy, events, cancel, socket_timeout) -> TransferResult:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if resume is None:
                callbacks.validator(ResourceValidator("http", (("etag", "e" * 64), ("length", 0), ("total", 1000))))
                callbacks.checkpoint(10, 0)
            self.resumed.append(resume is not None)
            self.started.release()
            while not self.release.is_set():
                if cancel.cancelled:
                    self.io_done.wait(10)
                    cancel.raise_if_cancelled()
                time.sleep(0.005)
            if self.fail_with is not None:
                raise self.fail_with
            media = work / "media.mp4"
            media.write_bytes(self.result_bytes)
            return TransferResult(media=media, size=len(self.result_bytes), protocol="http", extension="mp4")
        finally:
            with self.lock:
                self.active -= 1


def make_queue(tmp_path: Path, sources, **kwargs) -> tuple[OfflineQueue, LocalState, StaticNetwork]:
    state = open_local_state(tmp_path / "history.db")
    network = kwargs.pop("network", None) or StaticNetwork()
    queue = OfflineQueue(
        state,
        FsFileStore(tmp_path / "library"),
        sources,
        network=network,
        config=kwargs.pop("config", None) or fast_config(),
        **kwargs,
    )
    return queue, state, network


def wait_for(predicate: Callable[[], object], timeout: float = 15.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not reached in time; last value {value!r}")
