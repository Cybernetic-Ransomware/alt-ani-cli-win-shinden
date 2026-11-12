import json
import threading
import time
from pathlib import Path

import pytest

from alt_ani_core.cancel import CancelToken
from alt_ani_core.errors import OperationCancelled
from alt_ani_core.models import StreamProtocol, StreamVariant
from alt_ani_core.rules.resume import KIND_HLS, KIND_HTTP, checkpoint_of, with_checkpoint
from alt_ani_core.state import ResourceValidator
from alt_ani_core.transfer import (
    CheckpointPolicy,
    ResumeRefused,
    TransferCallbacks,
    parse_media_playlist,
    transfer,
)

from .servers import SECRET, Resource, ServerState, hls, random_body, replace_segment, serve

pytestmark = pytest.mark.loopback

NEVER_PERIODIC = CheckpointPolicy(interval_sec=1e9, every_bytes=1 << 40)


class Recorder:
    """Collects callbacks the way the executor persists them: the validator, then checkpoints into it."""

    def __init__(self, stop_at: int | None = None, cancel: CancelToken | None = None) -> None:
        self.validator: ResourceValidator | None = None
        self.events: list[str] = []
        self.cancel = cancel or CancelToken()
        self.stop_at = stop_at

    def on_validator(self, validator: ResourceValidator) -> None:
        self.events.append("validator")
        self.validator = validator

    def on_progress(self, done: int, _total: int | None) -> None:
        if "progress" not in self.events:
            self.events.append("progress")
        if self.stop_at is not None and done >= self.stop_at:
            self.cancel.cancel()

    def on_checkpoint(self, length: int, fragments: int) -> None:
        assert self.validator is not None
        self.events.append("checkpoint")
        self.validator = with_checkpoint(self.validator, length, fragments)

    def callbacks(self) -> TransferCallbacks:
        return TransferCallbacks(validator=self.on_validator, progress=self.on_progress, checkpoint=self.on_checkpoint)


def _progressive(url: str) -> StreamVariant:
    return StreamVariant(url=f"{url}/v.mp4?token={SECRET}", protocol=StreamProtocol.PROGRESSIVE, container="mp4")


def _hls_variant(url: str) -> StreamVariant:
    return StreamVariant(url=f"{url}/v.m3u8", protocol=StreamProtocol.HLS)


def _run(variant, work: Path, recorder: Recorder, resume=None, **kwargs):
    return transfer(
        variant,
        work,
        resume=resume,
        callbacks=recorder.callbacks(),
        policy=kwargs.pop("policy", NEVER_PERIODIC),
        cancel=recorder.cancel,
        socket_timeout=kwargs.pop("socket_timeout", 5),
        **kwargs,
    )


def _stopped_partial(variant, work: Path, stop_at: int) -> ResourceValidator:
    recorder = Recorder(stop_at=stop_at)
    with pytest.raises(OperationCancelled):
        _run(variant, work, recorder)
    assert recorder.validator is not None
    return recorder.validator


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.mark.unit
class TestProgressiveTransfer:
    def test_fresh_transfer_records_the_validator_before_any_byte(self, work):
        state = ServerState(resources={"/v.mp4": Resource(random_body(300_000), f'"e-{SECRET}"')})
        recorder = Recorder()
        with serve(state) as url:
            result = _run(_progressive(url), work, recorder)
        assert result.media.read_bytes() == state.resources["/v.mp4"].body
        assert recorder.events[0] == "validator"
        fields = dict(recorder.validator.fields)
        assert recorder.validator.kind == KIND_HTTP and fields["total"] == 300_000 and len(fields["etag"]) == 64
        assert SECRET not in json.dumps(fields)

    def test_stop_records_a_durable_checkpoint_within_the_partial(self, work):
        state = ServerState(resources={"/v.mp4": Resource(random_body(2_000_000), '"e1"')})
        with serve(state) as url:
            validator = _stopped_partial(_progressive(url), work, 200_000)
        length, _ = checkpoint_of(validator)
        assert 0 < length <= (work / "media.mp4.part").stat().st_size

    def test_same_entity_resumes_with_if_range_and_matches_the_whole_file(self, work):
        body = random_body(2_000_000)
        state = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
        with serve(state) as url:
            validator = _stopped_partial(_progressive(url), work, 200_000)
            state.log.clear()
            result = _run(_progressive(url), work, Recorder(), resume=validator)
        assert result.media.read_bytes() == body
        ranged = [entry for entry in state.log if entry[0] == "GET" and entry[2]]
        assert ranged and all(entry[3] == '"e1"' for entry in ranged)

    @pytest.mark.parametrize("change", ["entity", "ignore_range", "between_head_and_get"])
    def test_changed_or_unrangeable_resource_is_refused_and_the_partial_kept(self, work, change):
        body = random_body(2_000_000)
        state = ServerState(resources={"/v.mp4": Resource(body, '"e1"')})
        with serve(state) as url:
            validator = _stopped_partial(_progressive(url), work, 200_000)
            length, _ = checkpoint_of(validator)
            if change == "entity":
                state.resources["/v.mp4"] = Resource(random_body(2_000_000), '"e2"')
            elif change == "ignore_range":
                state.ignore_range = True
            else:

                def swap_after_head(method, _path):
                    if method == "HEAD":
                        state.resources["/v.mp4"] = Resource(random_body(2_000_000), '"e2"')

                state.on_request = swap_after_head
            with pytest.raises(ResumeRefused):
                _run(_progressive(url), work, Recorder(), resume=validator)
        part = work / "media.mp4.part"
        assert part.read_bytes()[:length] == body[:length]
        assert part.stat().st_size >= length

    @pytest.mark.parametrize("etag", [None, 'W/"weak"'])
    def test_without_a_strong_etag_nothing_is_resumed(self, work, etag):
        state = ServerState(resources={"/v.mp4": Resource(random_body(2_000_000), etag)})
        with serve(state) as url:
            validator = _stopped_partial(_progressive(url), work, 200_000)
            assert "etag" not in dict(validator.fields)
            with pytest.raises(ResumeRefused) as exc:
                _run(_progressive(url), work, Recorder(), resume=validator)
        assert exc.value.reason == "no_validator"

    def test_partial_shorter_than_its_checkpoint_is_refused(self, work):
        state = ServerState(resources={"/v.mp4": Resource(random_body(2_000_000), '"e1"')})
        with serve(state) as url:
            validator = _stopped_partial(_progressive(url), work, 200_000)
            (work / "media.mp4.part").write_bytes(b"short")
            with pytest.raises(ResumeRefused) as exc:
                _run(_progressive(url), work, Recorder(), resume=validator)
        assert exc.value.reason == "partial_shorter_than_checkpoint"

    def test_stalled_read_ends_within_the_socket_timeout_after_a_stop(self, work):
        state = ServerState(resources={"/v.mp4": Resource(random_body(2_000_000), '"e1"')}, stall_after=50_000)
        token = CancelToken()
        recorder = Recorder(cancel=token)
        with serve(state) as url:
            threading.Timer(0.5, token.cancel).start()
            started = time.monotonic()
            with pytest.raises(OperationCancelled):
                _run(_progressive(url), work, recorder, socket_timeout=2)
            elapsed = time.monotonic() - started
        assert elapsed < 2 + 0.5 + 3

    def test_periodic_checkpoints_follow_the_policy(self, work):
        state = ServerState(resources={"/v.mp4": Resource(random_body(3_000_000), '"e1"')})
        ticks = iter(range(10_000_000))
        policy = CheckpointPolicy(interval_sec=1e9, every_bytes=1_000_000, clock=lambda: next(ticks))
        recorder = Recorder()
        with serve(state) as url:
            _run(_progressive(url), work, recorder, policy=policy)
        assert 1 <= recorder.events.count("checkpoint") <= 3


@pytest.mark.unit
class TestHlsTransfer:
    def test_fresh_transfer_keeps_a_manifest_of_segment_etags(self, work):
        state = ServerState()
        whole = hls(state)
        recorder = Recorder()
        with serve(state) as url:
            result = _run(_hls_variant(url), work, recorder)
        assert result.media.read_bytes() == whole
        fields = dict(recorder.validator.fields)
        assert recorder.validator.kind == KIND_HLS and fields["segments"] == 6 and len(fields["manifest"]) == 64
        assert SECRET not in (work / "validators.json").read_text()

    @pytest.mark.parametrize("ytdl", ["kept", "missing", "lagging", "ahead"])
    def test_resume_rebuilds_ytdl_state_from_the_checkpoint(self, work, ytdl):
        state = ServerState()
        whole = hls(state)
        with serve(state) as url:
            validator = _stopped_partial(_hls_variant(url), work, 3 * 18800)
            length, fragments = checkpoint_of(validator)
            assert fragments >= 1 and length == fragments * 18800
            ytdl_file = work / "media.mp4.ytdl"
            if ytdl == "missing":
                ytdl_file.unlink()
            elif ytdl == "lagging":
                ytdl_file.write_text(json.dumps({"downloader": {"current_fragment": {"index": 0}}}))
            elif ytdl == "ahead":
                part = work / "media.mp4.part"
                part.write_bytes(part.read_bytes() + b"duplicate-fragment")
                ytdl_file.write_text(json.dumps({"downloader": {"current_fragment": {"index": fragments + 1}}}))
            result = _run(_hls_variant(url), work, Recorder(), resume=validator)
        assert result.media.read_bytes() == whole

    @pytest.mark.parametrize("index", [0, 5])
    def test_replaced_segment_with_the_same_playlist_is_refused(self, work, index):
        state = ServerState()
        whole = hls(state)
        with serve(state) as url:
            validator = _stopped_partial(_hls_variant(url), work, 3 * 18800)
            length, _ = checkpoint_of(validator)
            replace_segment(state, index)
            with pytest.raises(ResumeRefused) as exc:
                _run(_hls_variant(url), work, Recorder(), resume=validator)
        assert exc.value.reason == "segment_changed"
        part = (work / "media.mp4.part").read_bytes()
        # only verified segments were appended: the partial is a prefix of the original, up to the changed segment
        assert length <= len(part) <= index * 18800 or index == 0
        assert part == whole[: len(part)]

    def test_without_segment_etags_nothing_is_resumed(self, work):
        state = ServerState()
        hls(state, etags=False)
        with serve(state) as url:
            validator = _stopped_partial(_hls_variant(url), work, 3 * 18800)
            assert dict(validator.fields)["manifest"] == ""
            with pytest.raises(ResumeRefused) as exc:
                _run(_hls_variant(url), work, Recorder(), resume=validator)
        assert exc.value.reason == "no_validator"

    def test_tampered_manifest_is_refused(self, work):
        state = ServerState()
        hls(state)
        with serve(state) as url:
            validator = _stopped_partial(_hls_variant(url), work, 3 * 18800)
            (work / "validators.json").write_text('{"etags": []}')
            with pytest.raises(ResumeRefused) as exc:
                _run(_hls_variant(url), work, Recorder(), resume=validator)
        assert exc.value.reason == "manifest_unavailable"


@pytest.mark.unit
class TestMediaPlaylist:
    def test_structure_ignores_uri_tokens(self):
        a = parse_media_playlist("#EXTM3U\n#EXTINF:4.0,\nseg0.ts?tok=a\n", "http://h/x/v.m3u8")
        b = parse_media_playlist("#EXTM3U\n#EXTINF:4.0,\nseg0.ts?tok=b\n", "http://h/x/v.m3u8")
        assert a.structure == b.structure and a.segments == ("http://h/x/seg0.ts?tok=a",)

    @pytest.mark.parametrize("line", ['#EXT-X-KEY:METHOD=AES-128,URI="k"', "#EXT-X-BYTERANGE:10@0", '#EXT-X-MAP:URI="i.mp4"'])
    def test_encrypted_or_ranged_playlists_are_not_resumable(self, line):
        playlist = parse_media_playlist(f"#EXTM3U\n{line}\n#EXTINF:4.0,\nseg0.ts\n", "http://h/v.m3u8")
        assert not playlist.resumable
