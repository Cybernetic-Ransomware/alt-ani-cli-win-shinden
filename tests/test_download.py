import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from yt_dlp.utils import DownloadError

from alt_ani_cli.content import EXCEPTIONS_PL, error_text
from alt_ani_core import download
from alt_ani_core.cancel import CancelToken
from alt_ani_core.config import USER_AGENT, YTDLP_SOCKET_TIMEOUT_SEC
from alt_ani_core.errors import DownloadFailedError, DownloadTargetError, OperationCancelled
from alt_ani_core.events import DownloadLog, DownloadSaved, DownloadStarted, LegacyPartMoved
from alt_ani_core.models import EpisodeRow, SeriesRef, StreamProtocol, StreamVariant
from alt_ani_core.testing import RecordingSink

_EP = EpisodeRow(number=1, title="Ep", url="https://shinden.pl/episode/1")
_SERIES = SeriesRef(id="1", slug="show", title="Show", url="https://shinden.pl/series/1-show")


def _hls(url: str = "https://h/master.m3u8", headers: dict[str, str] | None = None) -> StreamVariant:
    return StreamVariant(url=url, protocol=StreamProtocol.HLS, headers=headers or {})


def _direct(url: str, container: str) -> StreamVariant:
    return StreamVariant(url=url, protocol=StreamProtocol.PROGRESSIVE, container=container)


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records opts and the on-disk state seen when the download starts."""

    instances: list[_FakeYDL] = []
    info: dict = {}
    fail_in: str | None = None
    extract_calls = 0
    payload: bytes | None = None
    before_progress: Callable[[], None] | None = None

    def __init__(self, opts: dict) -> None:
        self.opts = opts
        # Real YoutubeDL normalises a string outtmpl into params["outtmpl"]["default"].
        self.params = {**opts, "outtmpl": {"default": opts["outtmpl"]}}
        self.final_path: Path | None = None
        self.part_existed_at_download: bool | None = None
        self.exited = False
        _FakeYDL.instances.append(self)

    def __enter__(self) -> _FakeYDL:
        return self

    def __exit__(self, *exc) -> None:
        self.exited = True
        return None

    def extract_info(self, url: str, download: bool = True) -> dict:
        assert download is False
        _FakeYDL.extract_calls += 1
        if self.fail_in == "extract_info":
            raise DownloadError("ERROR: [generic] master: Unable to download webpage: timed out (https://h/m?token=SECRET)")
        return dict(self.info)

    def prepare_filename(self, info: dict) -> str:
        return self.params["outtmpl"]["default"].replace("%(ext)s", info["ext"]).replace("%%", "%")

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        self.final_path = Path(self.prepare_filename(info))
        part = self.final_path.with_name(self.final_path.name + ".part")
        self.part_existed_at_download = part.exists()
        if self.payload is not None:
            part.write_bytes(self.payload[:2])
        if (before_progress := type(self).before_progress) is not None:
            before_progress()
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading"})
        if self.fail_in == "process_ie_result":
            raise DownloadError("ERROR: fragment 2 not found, unable to continue (https://h/s2.ts?token=SECRET)")
        if self.payload is not None:
            part.unlink()
            self.final_path.write_bytes(self.payload)
        return info


@pytest.fixture
def fake_ydl():
    _FakeYDL.instances = []
    _FakeYDL.info = {"ext": "mp4", "protocol": "m3u8_native"}
    _FakeYDL.fail_in = None
    _FakeYDL.extract_calls = 0
    _FakeYDL.payload = None
    _FakeYDL.before_progress = None
    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        yield _FakeYDL


def _run(variant: StreamVariant, dest: Path, **kwargs) -> _FakeYDL:
    download.run(variant, _EP, _SERIES, dest, **kwargs)
    return _FakeYDL.instances[-1]


@pytest.mark.unit
class TestDownloadOptions:
    def test_no_external_downloader_even_with_ffmpeg_on_path(self, fake_ydl, tmp_path):
        with patch("shutil.which", return_value=r"C:\ffmpeg\bin\ffmpeg.exe"):
            ydl = _run(_hls(), tmp_path)
        assert "external_downloader" not in ydl.opts
        assert "external_downloader_args" not in ydl.opts

    def test_stream_headers_passed_through_unchanged(self, fake_ydl, tmp_path):
        headers = {"Referer": "https://embed.example/e/1", "Origin": "https://embed.example", "User-Agent": "UA/1.0"}
        ydl = _run(_hls(headers=headers), tmp_path)
        assert ydl.opts["http_headers"] == headers

    def test_missing_user_agent_falls_back_to_default(self, fake_ydl, tmp_path):
        ydl = _run(_hls(headers={"Referer": "https://r/"}), tmp_path)
        assert ydl.opts["http_headers"] == {"Referer": "https://r/", "User-Agent": USER_AGENT}

    def test_progress_lines_suppressed(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["noprogress"] is True

    def test_socket_timeout_bounds_stalled_connections(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["socket_timeout"] == YTDLP_SOCKET_TIMEOUT_SEC
        assert YTDLP_SOCKET_TIMEOUT_SEC > 0

    def test_start_is_reported_as_an_event(self, fake_ydl, tmp_path):
        sink = RecordingSink()
        download.run(
            _hls(), EpisodeRow(number=5.5, title="Ep", url="https://shinden.pl/episode/5"), _SERIES, tmp_path, events=sink
        )
        assert sink.events[0] == DownloadStarted(title="Show", episode_label="5.5", directory=str(tmp_path))


@pytest.mark.unit
class TestRetryPolicy:
    def test_request_retries_set_explicitly(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["retries"] == 10

    def test_fragment_retries_set_explicitly(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["fragment_retries"] == 10

    def test_failed_fragments_are_not_skipped(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["skip_unavailable_fragments"] is False

    def test_policy_applies_to_direct_downloads(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "mp4", "protocol": "https"}
        ydl = _run(_direct("https://cdn.example/video.mp4", "mp4"), tmp_path)
        assert (ydl.opts["retries"], ydl.opts["fragment_retries"], ydl.opts["skip_unavailable_fragments"]) == (10, 10, False)


@pytest.mark.unit
class TestOutputExtension:
    def test_direct_mp4_without_url_extension_avoids_unknown_video(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "unknown_video", "protocol": "https"}
        ydl = _run(_direct("https://cdn.example/d/abc123", "mp4"), tmp_path)
        assert ydl.final_path == tmp_path / "Show - ep1.mp4"

    def test_direct_mkv_keeps_mkv(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "unknown_video", "protocol": "https"}
        ydl = _run(_direct("https://cdn.example/d/abc123", "mkv"), tmp_path)
        assert ydl.final_path == tmp_path / "Show - ep1.mkv"

    def test_hls_uses_ytdlp_ext_not_m3u8(self, fake_ydl, tmp_path):
        ydl = _run(_hls(), tmp_path)
        assert ydl.opts["outtmpl"].endswith(".%(ext)s")
        assert ydl.final_path == tmp_path / "Show - ep1.mp4"


@pytest.mark.unit
class TestLegacyHlsPartGuard:
    def test_hls_without_part_touches_nothing(self, fake_ydl, tmp_path):
        _run(_hls(), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_hls_legacy_part_is_moved_aside_before_download(self, fake_ydl, tmp_path):
        part = tmp_path / "Show - ep1.mp4.part"
        part.write_bytes(b"legacy mp4 bytes")
        sink = RecordingSink()
        ydl = _run(_hls(), tmp_path, events=sink)
        backup = tmp_path / "Show - ep1.mp4.part.legacy-ffmpeg"
        assert ydl.part_existed_at_download is False
        assert backup.read_bytes() == b"legacy mp4 bytes"
        assert sink.of(LegacyPartMoved) == [LegacyPartMoved(path=str(backup))]

    def test_hls_part_with_ytdl_is_native_resume_state(self, fake_ydl, tmp_path):
        part = tmp_path / "Show - ep1.mp4.part"
        ytdl = tmp_path / "Show - ep1.mp4.ytdl"
        part.write_bytes(b"ts fragments")
        ytdl.write_text("{}")
        sink = RecordingSink()
        ydl = _run(_hls(), tmp_path, events=sink)
        assert ydl.part_existed_at_download is True
        assert sorted(p.name for p in tmp_path.iterdir()) == [part.name, ytdl.name]
        assert sink.of(LegacyPartMoved) == []

    def test_direct_part_without_ytdl_is_left_for_range_resume(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "mp4", "protocol": "https"}
        part = tmp_path / "Show - ep1.mp4.part"
        part.write_bytes(b"partial mp4")
        ydl = _run(_direct("https://cdn.example/video.mp4", "mp4"), tmp_path)
        assert ydl.part_existed_at_download is True
        assert [p.name for p in tmp_path.iterdir()] == [part.name]

    def test_existing_backup_is_not_overwritten(self, tmp_path):
        final = tmp_path / "Show - ep1.mp4"
        Path(f"{final}.part").write_bytes(b"newer legacy")
        Path(f"{final}.part.legacy-ffmpeg").write_bytes(b"older legacy")
        backup = download._quarantine_legacy_hls_part(final)
        assert backup == tmp_path / "Show - ep1.mp4.part.legacy-ffmpeg.1"
        assert backup.read_bytes() == b"newer legacy"
        assert Path(f"{final}.part.legacy-ffmpeg").read_bytes() == b"older legacy"
        assert not Path(f"{final}.part").exists()


@pytest.mark.unit
class TestDownloadFailure:
    @pytest.mark.parametrize("stage", ["extract_info", "process_ie_result"])
    def test_ytdlp_error_becomes_download_failed_error(self, fake_ydl, tmp_path, stage):
        fake_ydl.fail_in = stage
        with pytest.raises(DownloadFailedError) as info:
            _run(_hls(), tmp_path)
        assert info.value.code == "download.failed"
        assert info.value.params == {}
        assert "token" not in str(info.value)
        assert "http" not in str(info.value)

    @pytest.mark.parametrize("stage", ["extract_info", "process_ie_result"])
    def test_failure_does_not_report_saved(self, fake_ydl, tmp_path, stage):
        fake_ydl.fail_in = stage
        (tmp_path / "Show - ep1.mp4").write_bytes(b"older complete file")
        sink = RecordingSink()
        with pytest.raises(DownloadFailedError):
            _run(_hls(), tmp_path, events=sink)
        assert sink.of(DownloadSaved) == []

    def test_original_ytdlp_exception_is_not_chained(self, fake_ydl, tmp_path):
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError) as info:
            _run(_hls(), tmp_path)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None

    def test_gc_runs_after_context_exit_with_no_active_exception(self, fake_ydl, tmp_path):
        fake_ydl.fail_in = "process_ie_result"
        seen: list[tuple[bool, object]] = []

        def collect() -> int:
            seen.append((_FakeYDL.instances[-1].exited, sys.exception()))
            return 0

        with patch.object(download.gc, "collect", side_effect=collect), pytest.raises(DownloadFailedError):
            _run(_hls(), tmp_path)
        assert seen == [(True, None)]

    def test_success_does_not_force_gc(self, fake_ydl, tmp_path):
        with patch.object(download.gc, "collect") as collect:
            _run(_hls(), tmp_path)
        collect.assert_not_called()

    def test_success_reports_saved_path(self, fake_ydl, tmp_path):
        class _WritingYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                super().process_ie_result(info, download)
                self.final_path.write_bytes(b"mp4")
                return info

        sink = RecordingSink()
        with patch("yt_dlp.YoutubeDL", _WritingYDL):
            download.run(_hls(), _EP, _SERIES, tmp_path, events=sink)
        assert sink.of(DownloadSaved) == [DownloadSaved(path=str(tmp_path / "Show - ep1.mp4"))]


_OLD = b"OLD complete episode"
_NEW = b"NEW complete episode"


def _existing_final(dest: Path) -> Path:
    final = dest / "Show - ep1.mp4"
    final.write_bytes(_OLD)
    return final


def _overwrite(dest: Path, **kwargs) -> bool:
    return download.run(_hls(), _EP, _SERIES, dest, confirm_overwrite=lambda _: True, **kwargs)


@pytest.mark.unit
class TestExistingFinalWithoutOverwrite:
    def test_no_callback_leaves_existing_final_to_ytdlp_no_overwrite(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        assert download.run(_hls(), _EP, _SERIES, tmp_path) is True
        assert fake_ydl.instances[-1].final_path == final
        assert "overwrites" not in fake_ydl.instances[-1].opts
        assert final.read_bytes() == _OLD

    def test_declined_overwrite_skips_download_and_keeps_file(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        sink = RecordingSink()
        assert download.run(_hls(), _EP, _SERIES, tmp_path, confirm_overwrite=lambda _: False, events=sink) is False
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert sink.of(DownloadSaved) == []

    def test_callback_receives_exact_final_path(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        seen: list[Path] = []
        download.run(_hls(), _EP, _SERIES, tmp_path, confirm_overwrite=lambda p: seen.append(p) or False)
        assert seen == [final]

    def test_callback_not_consulted_when_final_missing(self, fake_ydl, tmp_path):
        fake_ydl.payload = _NEW
        asked: list[Path] = []
        download.run(_hls(), _EP, _SERIES, tmp_path, confirm_overwrite=lambda p: asked.append(p) or True)
        assert asked == []
        assert (tmp_path / "Show - ep1.mp4").read_bytes() == _NEW


@pytest.mark.unit
class TestSafeOverwrite:
    def test_success_replaces_final_with_new_content(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        assert _overwrite(tmp_path) is True
        assert final.read_bytes() == _NEW

    def test_download_targets_staging_not_final(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        _overwrite(tmp_path)
        assert fake_ydl.instances[-1].final_path == tmp_path / "Show - ep1.redownload.mp4"

    def test_success_leaves_no_staging_garbage(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        _overwrite(tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == [final.name]

    def test_success_reports_final_path(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        sink = RecordingSink()
        _overwrite(tmp_path, events=sink)
        assert sink.of(DownloadSaved) == [DownloadSaved(path=str(final))]

    def test_failure_keeps_old_final_bytes(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD

    def test_failure_discards_staging_partials(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == [final.name]

    def test_ytdlp_error_during_overwrite_is_contained(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError) as info:
            _overwrite(tmp_path)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None
        assert info.value.code == "download.failed"
        assert "token" not in str(info.value)

    def test_gc_runs_before_staging_cleanup(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        fake_ydl.fail_in = "process_ie_result"
        staging_part = tmp_path / "Show - ep1.redownload.mp4.part"
        seen: list[tuple[bool, object, bool]] = []

        def collect() -> int:
            seen.append((_FakeYDL.instances[-1].exited, sys.exception(), staging_part.exists()))
            return 0

        with patch.object(download.gc, "collect", side_effect=collect), pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert seen == [(True, None, True)]
        assert not staging_part.exists()

    def test_extract_info_runs_once_around_the_decision(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        calls_at_decision: list[int] = []

        def confirm(_: Path) -> bool:
            calls_at_decision.append(_FakeYDL.extract_calls)
            return True

        download.run(_hls(), _EP, _SERIES, tmp_path, confirm_overwrite=confirm)
        assert calls_at_decision == [1]
        assert _FakeYDL.extract_calls == 1
        assert len(_FakeYDL.instances) == 1

    def test_replace_failure_keeps_old_final_and_discards_new_copy(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        with (
            patch.object(download.os, "replace", side_effect=PermissionError(13, "in use")),
            pytest.raises(DownloadTargetError) as info,
        ):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert info.value.code == "download.replace_failed"
        assert info.value.params == {"path": str(final)}
        assert str(final) in str(info.value)
        assert info.value.__cause__ is None

    def test_staging_path_mismatch_aborts_before_download(self, fake_ydl, tmp_path):
        class _StagingIgnoredYDL(_FakeYDL):
            def prepare_filename(self, info: dict) -> str:
                return self.opts["outtmpl"].replace("%(ext)s", info["ext"])

        final = _existing_final(tmp_path)
        with patch("yt_dlp.YoutubeDL", _StagingIgnoredYDL), pytest.raises(DownloadTargetError) as info:
            _overwrite(tmp_path)
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert info.value.code == "download.staging_mismatch"
        assert info.value.params == {"path": str(final)}
        assert str(final) in str(info.value)

    def test_missing_staged_file_after_success_is_a_failure(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        sink = RecordingSink()
        with pytest.raises(DownloadFailedError) as info:
            _overwrite(tmp_path, events=sink)
        assert info.value.code == "download.failed"
        assert final.read_bytes() == _OLD
        assert sink.of(DownloadSaved) == []

    def test_staging_template_escapes_percent(self, fake_ydl, tmp_path):
        dest = tmp_path / "100%"
        dest.mkdir()
        final = _existing_final(dest)
        fake_ydl.payload = _NEW
        _overwrite(dest)
        assert final.read_bytes() == _NEW


@pytest.mark.unit
class TestOverwritePartialFiles:
    def test_stale_staging_partials_are_not_resumed(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        (tmp_path / "Show - ep1.redownload.mp4.part").write_bytes(b"other source ts")
        (tmp_path / "Show - ep1.redownload.mp4.ytdl").write_text("{}")
        (tmp_path / "Show - ep1.redownload.mp4").write_bytes(b"crashed before promote")
        fake_ydl.payload = _NEW
        _overwrite(tmp_path)
        assert fake_ydl.instances[-1].part_existed_at_download is False

    def test_final_native_resume_state_is_left_alone(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        part = tmp_path / "Show - ep1.mp4.part"
        ytdl = tmp_path / "Show - ep1.mp4.ytdl"
        part.write_bytes(b"ts fragments")
        ytdl.write_text("{}")
        fake_ydl.payload = _NEW
        _overwrite(tmp_path)
        assert part.read_bytes() == b"ts fragments"
        assert ytdl.read_text() == "{}"
        assert final.read_bytes() == _NEW

    def test_final_legacy_part_is_not_spliced_into_redownload(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        legacy = tmp_path / "Show - ep1.mp4.part"
        legacy.write_bytes(b"legacy mp4 bytes")
        fake_ydl.payload = _NEW
        _overwrite(tmp_path)
        assert fake_ydl.instances[-1].part_existed_at_download is False
        assert legacy.read_bytes() == b"legacy mp4 bytes"
        assert final.read_bytes() == _NEW

    def test_discard_ignores_similarly_named_episodes(self, tmp_path):
        final = tmp_path / "Show - ep1.mp4"
        keep = [tmp_path / "Show - ep10.redownload.mp4", tmp_path / "Show - ep1.mp4.part", final]
        for p in keep:
            p.write_bytes(b"x")
        (tmp_path / "Show - ep1.redownload.temp.mp4").write_bytes(b"fixup temp")
        download._discard_staging(final)
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in keep)


def _unlink_fails_for(name: str):
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs) -> None:
        if self.name == name:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    return patch.object(Path, "unlink", unlink)


@pytest.mark.unit
class TestStrictStagingPreflight:
    @pytest.mark.parametrize(
        "locked",
        ["Show - ep1.redownload.mp4", "Show - ep1.redownload.mp4.part", "Show - ep1.redownload.mp4.ytdl"],
    )
    def test_undeletable_stale_staging_aborts_before_download(self, fake_ydl, tmp_path, locked):
        final = _existing_final(tmp_path)
        (tmp_path / locked).write_bytes(b"stale staging")
        fake_ydl.payload = _NEW
        with (
            _unlink_fails_for(locked),
            pytest.raises(DownloadTargetError) as info,
        ):
            _overwrite(tmp_path)
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert (tmp_path / locked).read_bytes() == b"stale staging"
        assert info.value.code == "download.staging_locked"
        assert info.value.params == {"path": str(tmp_path / locked)}
        assert locked in str(info.value)

    def test_cleanup_after_failed_attempt_stays_best_effort(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        fake_ydl.fail_in = "process_ie_result"
        with _unlink_fails_for("Show - ep1.redownload.mp4.part"), pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD


def _logged(method: str, message: str) -> list[str]:
    sink = RecordingSink()
    getattr(download._EventLogger(sink), method)(message)
    return [event.redacted_line for event in sink.of(DownloadLog)]


@pytest.mark.unit
class TestEventLoggerRedaction:
    def test_secret_query_value_never_emitted(self):
        (line,) = _logged("info", "[hlsnative] Downloading m3u8 manifest https://cdn.example/hls/master.m3u8?token=SECRET")
        assert "SECRET" not in line
        assert "<redacted>" in line
        assert "cdn.example/hls/master.m3u8?token=" in line

    def test_long_line_is_redacted_whole_and_left_untruncated(self):
        head = "x" * 40 + " https://h.example/m.m3u8?token="
        assert _logged("info", head + "SECRET_TAIL_VALUE_THAT_IS_LONG") == [head + "<redacted>"]

    @pytest.mark.parametrize(("method", "prefix"), [("warning", "[warn] "), ("error", "[error] ")])
    def test_warning_and_error_redacted(self, method, prefix):
        (line,) = _logged(method, "fragment failed: master.m3u8?token=SECRET&expires=123")
        assert line.startswith(prefix)
        assert "SECRET" not in line and "123" not in line
        assert "master.m3u8?token=<redacted>&expires=<redacted>" in line

    def test_suppressed_prefixes_still_suppressed(self):
        assert _logged("info", "[generic] Extracting URL: https://h/m?token=SECRET") == []

    def test_debug_chatter_is_dropped_but_plain_debug_lines_pass(self):
        assert _logged("debug", "[debug] Command-line config: []") == []
        assert _logged("debug", "[download] 100% of 5.00MiB") == ["[download] 100% of 5.00MiB"]

    def test_real_ytdlp_timeout_message_does_not_leak(self):
        msg = (
            "ERROR: [generic] master: Unable to download webpage: "
            "https://host.example/hls/master.m3u8?token=THIS_MUST_NOT_LEAK&expires=999 (caused by TransportError('timed out'))"
        )
        (line,) = _logged("error", msg)
        assert "THIS_MUST_NOT_LEAK" not in line
        assert "999" not in line
        assert "host.example/hls/master.m3u8?token=<redacted>&expires=<redacted>" in line
        assert "timed out" in line

    def test_local_paths_untouched(self):
        (line,) = _logged("info", r"[download] Destination: C:\Users\Test\Videos\Show - ep4.mp4")
        assert r"C:\Users\Test\Videos\Show - ep4.mp4" in line

    def test_run_wires_the_logger_to_the_event_sink(self, fake_ydl, tmp_path):
        sink = RecordingSink()
        ydl = _run(_hls(), tmp_path, events=sink)
        ydl.opts["logger"].warning("retrying https://h/s1.ts?token=SECRET")
        assert sink.of(DownloadLog) == [DownloadLog(redacted_line="[warn] retrying https://h/s1.ts?token=<redacted>")]


@pytest.mark.unit
class TestRedactionDoesNotTouchTransport:
    def test_signed_url_and_headers_reach_ytdlp_verbatim(self, fake_ydl, tmp_path):
        url = "https://cdn.example/hls/master.m3u8?token=SECRET&expires=999"
        headers = {"Referer": "https://embed.example/e/1?token=REF", "Cookie": "session=S", "User-Agent": "UA/1.0"}
        seen: list[str] = []
        original = _FakeYDL.extract_info

        def spy(self, u, download=True):
            seen.append(u)
            return original(self, u, download)

        with patch.object(_FakeYDL, "extract_info", spy):
            ydl = _run(_hls(url, dict(headers)), tmp_path)
        assert seen == [url]
        assert ydl.opts["http_headers"] == headers


@pytest.mark.unit
class TestCancellation:
    def test_already_cancelled_token_raises_before_any_download(self, fake_ydl, tmp_path):
        token = CancelToken()
        token.cancel()
        sink = RecordingSink()
        with pytest.raises(OperationCancelled):
            _run(_hls(), tmp_path, cancel=token, events=sink)
        assert fake_ydl.extract_calls == 0
        assert fake_ydl.instances[-1].final_path is None
        assert list(tmp_path.iterdir()) == []
        assert sink.of(DownloadSaved) == []

    def test_cancel_between_probe_and_download_starts_nothing(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        token = CancelToken()

        def confirm(_: Path) -> bool:
            token.cancel()
            return True

        with pytest.raises(OperationCancelled):
            download.run(_hls(), _EP, _SERIES, tmp_path, confirm_overwrite=confirm, cancel=token)
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]

    def test_cancel_from_progress_hook_discards_redownload_and_keeps_final(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW
        staging_part = tmp_path / "Show - ep1.redownload.mp4.part"
        existed_at_cancel: list[bool] = []
        fake_ydl.before_progress = lambda: (existed_at_cancel.append(staging_part.exists()), token.cancel())
        sink = RecordingSink()
        with pytest.raises(OperationCancelled):
            _overwrite(tmp_path, cancel=token, events=sink)
        assert existed_at_cancel == [True]
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert sink.of(DownloadSaved) == []

    def test_cancelled_redownload_is_not_reported_as_download_failure(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW
        fake_ydl.before_progress = token.cancel
        with pytest.raises(OperationCancelled) as info:
            _overwrite(tmp_path, cancel=token)
        assert not isinstance(info.value, Exception)

    def test_gc_runs_after_context_exit_before_cancelled_staging_cleanup(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW
        fake_ydl.before_progress = token.cancel
        staging_part = tmp_path / "Show - ep1.redownload.mp4.part"
        seen: list[tuple[bool, bool]] = []

        def collect() -> int:
            seen.append((_FakeYDL.instances[-1].exited, staging_part.exists()))
            return 0

        with patch.object(download.gc, "collect", side_effect=collect), pytest.raises(OperationCancelled):
            _overwrite(tmp_path, cancel=token)
        assert seen == [(True, True)]
        assert not staging_part.exists()

    def test_cancelled_fresh_download_keeps_its_partial_for_resume(self, fake_ydl, tmp_path):
        token = CancelToken()
        fake_ydl.payload = _NEW
        fake_ydl.before_progress = token.cancel
        with pytest.raises(OperationCancelled):
            _run(_hls(), tmp_path, cancel=token)
        assert [p.name for p in tmp_path.iterdir()] == ["Show - ep1.mp4.part"]

    def test_cancel_after_last_progress_callback_keeps_old_final(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW
        staged: list[bool] = []

        class _PostprocessCancelYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                super().process_ie_result(info, download)
                staged.append(self.final_path is not None and self.final_path.read_bytes() == _NEW)
                token.cancel()
                return info

        sink = RecordingSink()
        with patch("yt_dlp.YoutubeDL", _PostprocessCancelYDL), pytest.raises(OperationCancelled):
            _overwrite(tmp_path, cancel=token, events=sink)
        assert staged == [True]
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert sink.of(DownloadSaved) == []

    def test_gc_runs_after_context_exit_before_post_download_cancel_cleanup(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW
        staging = tmp_path / "Show - ep1.redownload.mp4"

        class _PostprocessCancelYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                super().process_ie_result(info, download)
                token.cancel()
                return info

        seen: list[tuple[bool, bool]] = []

        def collect() -> int:
            seen.append((_FakeYDL.instances[-1].exited, staging.exists()))
            return 0

        with (
            patch("yt_dlp.YoutubeDL", _PostprocessCancelYDL),
            patch.object(download.gc, "collect", side_effect=collect),
            pytest.raises(OperationCancelled),
        ):
            _overwrite(tmp_path, cancel=token)
        assert seen == [(True, True)]
        assert not staging.exists()

    def test_cancel_after_fresh_download_is_not_reported_as_saved(self, fake_ydl, tmp_path):
        token = CancelToken()
        fake_ydl.payload = _NEW

        class _PostprocessCancelYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                super().process_ie_result(info, download)
                token.cancel()
                return info

        sink = RecordingSink()
        with patch("yt_dlp.YoutubeDL", _PostprocessCancelYDL), pytest.raises(OperationCancelled):
            _run(_hls(), tmp_path, cancel=token, events=sink)
        assert sink.of(DownloadSaved) == []

    def test_downloader_error_racing_cancel_is_reported_as_cancel(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        token = CancelToken()
        fake_ydl.payload = _NEW

        class _ErrorOnCancelYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                self.final_path = Path(self.prepare_filename(info))
                self.final_path.with_name(self.final_path.name + ".part").write_bytes(b"NE")
                token.cancel()
                raise DownloadError("ERROR: interrupted while writing fragment")

        sink = RecordingSink()
        with patch("yt_dlp.YoutubeDL", _ErrorOnCancelYDL), pytest.raises(OperationCancelled) as info:
            _overwrite(tmp_path, cancel=token, events=sink)
        assert info.value.__context__ is None
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert sink.of(DownloadSaved) == []

    def test_downloader_error_without_cancel_stays_a_failure(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError):
            _overwrite(tmp_path, cancel=CancelToken())

    def test_uncancelled_token_lets_the_download_finish(self, fake_ydl, tmp_path):
        fake_ydl.payload = _NEW
        _run(_hls(), tmp_path, cancel=CancelToken())
        assert (tmp_path / "Show - ep1.mp4").read_bytes() == _NEW


@pytest.mark.unit
class TestCliRendersDownloadErrorsInPolish:
    def test_download_failed(self, fake_ydl, tmp_path):
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError) as info:
            _run(_hls(), tmp_path)
        assert str(info.value) == "Download failed."
        assert error_text(info.value) == EXCEPTIONS_PL["download"]["failed"]

    def test_replace_failed(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        with (
            patch.object(download.os, "replace", side_effect=PermissionError(13, "in use")),
            pytest.raises(DownloadTargetError) as info,
        ):
            _overwrite(tmp_path)
        assert error_text(info.value) == EXCEPTIONS_PL["download"]["replace_failed"].format(path=final)

    def test_staging_locked(self, fake_ydl, tmp_path):
        _existing_final(tmp_path)
        locked = tmp_path / "Show - ep1.redownload.mp4.part"
        locked.write_bytes(b"stale staging")
        with _unlink_fails_for(locked.name), pytest.raises(DownloadTargetError) as info:
            _overwrite(tmp_path)
        assert error_text(info.value) == EXCEPTIONS_PL["download"]["staging_locked"].format(path=locked)

    def test_staging_mismatch(self, fake_ydl, tmp_path):
        class _StagingIgnoredYDL(_FakeYDL):
            def prepare_filename(self, info: dict) -> str:
                return self.opts["outtmpl"].replace("%(ext)s", info["ext"])

        final = _existing_final(tmp_path)
        with patch("yt_dlp.YoutubeDL", _StagingIgnoredYDL), pytest.raises(DownloadTargetError) as info:
            _overwrite(tmp_path)
        assert error_text(info.value) == EXCEPTIONS_PL["download"]["staging_mismatch"].format(path=final)

    @pytest.mark.parametrize("key", ["failed", "replace_failed", "staging_locked", "staging_mismatch"])
    def test_polish_text_differs_from_the_engine_text(self, key):
        assert EXCEPTIONS_PL["download"][key] != download.text(f"download.{key}", path="{path}")
