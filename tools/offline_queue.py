"""Try the offline executor by hand on throwaway data: serve a local file, queue it, watch it download."""

import argparse
import hashlib
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alt_ani_core.events import EngineEvent
from alt_ani_core.history.local import open_local_state
from alt_ani_core.models import EpisodeRow, PlayerEntry, SeriesRef, Stream, StreamProtocol, StreamVariant
from alt_ani_core.services.offline import OfflineQueue
from alt_ani_core.services.offline.network import StaticNetwork
from alt_ani_core.state import DownloadCriteria, JobState, NetworkKind
from alt_ani_core.storage.fs import FsFileStore

SERIES = SeriesRef(id="0", slug="local", title="Local Test", url="https://shinden.pl/series/0-local")


class _Handler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        path = Path(self.translate_path(self.path))
        if path.is_file():
            stat = path.stat()
            self.send_header("ETag", f'"{hashlib.sha256(f"{stat.st_size}-{stat.st_mtime_ns}".encode()).hexdigest()[:16]}"')
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, *_args) -> None:
        pass


class _Sources:
    def __init__(self, url: str) -> None:
        self._entry = PlayerEntry(online_id="1", player="local", lang_audio="jp", lang_subs="pl", max_res="1080p")
        self._url = url

    @property
    def entries(self):
        return (self._entry,)

    def open(self, episode, *, grant, cancel):
        return self

    def resolve(self, entry, *, fresh, cancel) -> Stream:
        variant = StreamVariant(url=self._url, protocol=StreamProtocol.PROGRESSIVE, label="720p", container="mp4")
        return Stream(variants=(variant,))

    def network_changed(self, network: NetworkKind) -> None:
        pass


class _Printer:
    def emit(self, event: EngineEvent) -> None:
        print(f"  event: {event}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--keep", type=Path, default=None)
    args = parser.parse_args()

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="alt-ani-offline-"))
    workdir.mkdir(parents=True, exist_ok=True)
    directory = str(args.video.resolve().parent)
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: _Handler(*a, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/{args.video.name}"

    state = open_local_state(workdir / "history.db")
    queue = OfflineQueue(
        state, FsFileStore(workdir / "library"), _Sources(url), network=StaticNetwork(NetworkKind.WIFI), events=_Printer()
    )
    episodes = [EpisodeRow(number=n, title=f"Episode {n}", url=f"{SERIES.url}/{n}") for n in range(1, args.episodes + 1)]
    added = queue.enqueue(SERIES, episodes, DownloadCriteria())
    print(f"data in {workdir}; added {len(added.added)}, skipped {len(added.skipped)}")
    print(f"recovery: {queue.start()}")
    try:
        while True:
            jobs = state.queue.jobs()
            print("  " + ", ".join(f"ep{j.key.number:g}:{j.state.value}:{j.bytes_done or 0}" for j in jobs))
            if all(j.state in (JobState.COMPLETED, JobState.FAILED, JobState.WAITING) for j in jobs):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        print(f"closed cleanly: {queue.close()}")
        server.shutdown()
    for item in state.library.items():
        print(f"  {item.status.value}: {workdir / 'library' / item.spec.locator.key} ({item.spec.size_bytes} B)")


if __name__ == "__main__":
    main()
