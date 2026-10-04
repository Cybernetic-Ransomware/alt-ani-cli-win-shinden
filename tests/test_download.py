import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from yt_dlp.utils import DownloadError

from alt_ani_cli import download
from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.errors import DownloadFailedError
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EpisodeRow, SeriesRef

_EP = EpisodeRow(number=1, title="Ep", url="https://shinden.pl/episode/1")
_SERIES = SeriesRef(id="1", slug="show", title="Show", url="https://shinden.pl/series/1-show")


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records opts and the on-disk state seen when the download starts."""

    instances: list[_FakeYDL] = []
    info: dict = {}
    fail_in: str | None = None
    extract_calls = 0
    payload: bytes | None = None

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
    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        yield _FakeYDL


def _run(stream: Stream, dest: Path) -> _FakeYDL:
    download.run(stream, _EP, _SERIES, dest_dir=dest)
    return _FakeYDL.instances[-1]


@pytest.mark.unit
class TestDownloadOptions:
    def test_no_external_downloader_even_with_ffmpeg_on_path(self, fake_ydl, tmp_path):
        with patch("shutil.which", return_value=r"C:\ffmpeg\bin\ffmpeg.exe"):
            ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert "external_downloader" not in ydl.opts
        assert "external_downloader_args" not in ydl.opts

    def test_stream_headers_passed_through_unchanged(self, fake_ydl, tmp_path):
        headers = {"Referer": "https://embed.example/e/1", "Origin": "https://embed.example", "User-Agent": "UA/1.0"}
        ydl = _run(Stream(url="https://h/master.m3u8", headers=headers, ext="m3u8"), tmp_path)
        assert ydl.opts["http_headers"] == headers

    def test_missing_user_agent_falls_back_to_default(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", headers={"Referer": "https://r/"}, ext="m3u8"), tmp_path)
        assert ydl.opts["http_headers"] == {"Referer": "https://r/", "User-Agent": USER_AGENT}

    def test_progress_lines_suppressed(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.opts["noprogress"] is True


@pytest.mark.unit
class TestRetryPolicy:
    def test_request_retries_set_explicitly(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.opts["retries"] == 10

    def test_fragment_retries_set_explicitly(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.opts["fragment_retries"] == 10

    def test_failed_fragments_are_not_skipped(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.opts["skip_unavailable_fragments"] is False

    def test_policy_applies_to_direct_downloads(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "mp4", "protocol": "https"}
        ydl = _run(Stream(url="https://cdn.example/video.mp4", ext="mp4"), tmp_path)
        assert (ydl.opts["retries"], ydl.opts["fragment_retries"], ydl.opts["skip_unavailable_fragments"]) == (10, 10, False)


@pytest.mark.unit
class TestOutputExtension:
    def test_direct_mp4_without_url_extension_avoids_unknown_video(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "unknown_video", "protocol": "https"}
        ydl = _run(Stream(url="https://cdn.example/d/abc123", ext="mp4"), tmp_path)
        assert ydl.final_path == tmp_path / "Show - ep1.mp4"

    def test_direct_mkv_keeps_mkv(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "unknown_video", "protocol": "https"}
        ydl = _run(Stream(url="https://cdn.example/d/abc123", ext="mkv"), tmp_path)
        assert ydl.final_path == tmp_path / "Show - ep1.mkv"

    def test_hls_uses_ytdlp_ext_not_m3u8(self, fake_ydl, tmp_path):
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.opts["outtmpl"].endswith(".%(ext)s")
        assert ydl.final_path == tmp_path / "Show - ep1.mp4"


@pytest.mark.unit
class TestLegacyHlsPartGuard:
    def test_hls_without_part_touches_nothing(self, fake_ydl, tmp_path):
        _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_hls_legacy_part_is_moved_aside_before_download(self, fake_ydl, tmp_path):
        part = tmp_path / "Show - ep1.mp4.part"
        part.write_bytes(b"legacy mp4 bytes")
        with patch.object(download.progress, "warn") as warn:
            ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        backup = tmp_path / "Show - ep1.mp4.part.legacy-ffmpeg"
        assert ydl.part_existed_at_download is False
        assert backup.read_bytes() == b"legacy mp4 bytes"
        assert str(backup) in warn.call_args.args[0]

    def test_hls_part_with_ytdl_is_native_resume_state(self, fake_ydl, tmp_path):
        part = tmp_path / "Show - ep1.mp4.part"
        ytdl = tmp_path / "Show - ep1.mp4.ytdl"
        part.write_bytes(b"ts fragments")
        ytdl.write_text("{}")
        ydl = _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert ydl.part_existed_at_download is True
        assert sorted(p.name for p in tmp_path.iterdir()) == [part.name, ytdl.name]

    def test_direct_part_without_ytdl_is_left_for_range_resume(self, fake_ydl, tmp_path):
        fake_ydl.info = {"ext": "mp4", "protocol": "https"}
        part = tmp_path / "Show - ep1.mp4.part"
        part.write_bytes(b"partial mp4")
        ydl = _run(Stream(url="https://cdn.example/video.mp4", ext="mp4"), tmp_path)
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
            _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert "token" not in str(info.value)
        assert "http" not in str(info.value)

    @pytest.mark.parametrize("stage", ["extract_info", "process_ie_result"])
    def test_failure_does_not_report_saved(self, fake_ydl, tmp_path, stage):
        fake_ydl.fail_in = stage
        (tmp_path / "Show - ep1.mp4").write_bytes(b"older complete file")
        with patch.object(download.progress, "success") as success, pytest.raises(DownloadFailedError):
            _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        success.assert_not_called()

    def test_original_ytdlp_exception_is_not_chained(self, fake_ydl, tmp_path):
        fake_ydl.fail_in = "process_ie_result"
        with pytest.raises(DownloadFailedError) as info:
            _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert info.value.__cause__ is None
        assert info.value.__context__ is None

    def test_gc_runs_after_context_exit_with_no_active_exception(self, fake_ydl, tmp_path):
        fake_ydl.fail_in = "process_ie_result"
        seen: list[tuple[bool, object]] = []

        def collect() -> int:
            seen.append((_FakeYDL.instances[-1].exited, sys.exception()))
            return 0

        with patch.object(download.gc, "collect", side_effect=collect), pytest.raises(DownloadFailedError):
            _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        assert seen == [(True, None)]

    def test_success_does_not_force_gc(self, fake_ydl, tmp_path):
        with patch.object(download.gc, "collect") as collect:
            _run(Stream(url="https://h/master.m3u8", ext="m3u8"), tmp_path)
        collect.assert_not_called()

    def test_success_reports_saved_path(self, fake_ydl, tmp_path):
        class _WritingYDL(_FakeYDL):
            def process_ie_result(self, info: dict, download: bool = True) -> dict:
                super().process_ie_result(info, download)
                self.final_path.write_bytes(b"mp4")
                return info

        with patch("yt_dlp.YoutubeDL", _WritingYDL), patch.object(download.progress, "success") as success:
            download.run(Stream(url="https://h/master.m3u8", ext="m3u8"), _EP, _SERIES, dest_dir=tmp_path)
        assert str(tmp_path / "Show - ep1.mp4") in success.call_args.args[0]


_HLS = Stream(url="https://h/master.m3u8", ext="m3u8")
_OLD = b"OLD complete episode"
_NEW = b"NEW complete episode"


def _existing_final(dest: Path) -> Path:
    final = dest / "Show - ep1.mp4"
    final.write_bytes(_OLD)
    return final


def _overwrite(dest: Path) -> bool:
    return download.run(_HLS, _EP, _SERIES, dest_dir=dest, confirm_overwrite=lambda _: True)


@pytest.mark.unit
class TestExistingFinalWithoutOverwrite:
    def test_no_callback_leaves_existing_final_to_ytdlp_no_overwrite(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        assert download.run(_HLS, _EP, _SERIES, dest_dir=tmp_path) is True
        assert fake_ydl.instances[-1].final_path == final
        assert "overwrites" not in fake_ydl.instances[-1].opts
        assert final.read_bytes() == _OLD

    def test_declined_overwrite_skips_download_and_keeps_file(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        assert download.run(_HLS, _EP, _SERIES, dest_dir=tmp_path, confirm_overwrite=lambda _: False) is False
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]

    def test_callback_receives_exact_final_path(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        seen: list[Path] = []
        download.run(_HLS, _EP, _SERIES, dest_dir=tmp_path, confirm_overwrite=lambda p: seen.append(p) or False)
        assert seen == [final]

    def test_callback_not_consulted_when_final_missing(self, fake_ydl, tmp_path):
        fake_ydl.payload = _NEW
        asked: list[Path] = []
        download.run(_HLS, _EP, _SERIES, dest_dir=tmp_path, confirm_overwrite=lambda p: asked.append(p) or True)
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
        with patch.object(download.progress, "success") as success:
            _overwrite(tmp_path)
        assert str(final) in success.call_args.args[0]

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

        download.run(_HLS, _EP, _SERIES, dest_dir=tmp_path, confirm_overwrite=confirm)
        assert calls_at_decision == [1]
        assert _FakeYDL.extract_calls == 1
        assert len(_FakeYDL.instances) == 1

    def test_replace_failure_keeps_old_final_and_discards_new_copy(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        with (
            patch.object(download.os, "replace", side_effect=PermissionError(13, "in use")),
            patch.object(download.progress, "error") as error,
            pytest.raises(DownloadFailedError),
        ):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD
        assert [p.name for p in tmp_path.iterdir()] == [final.name]
        assert str(final) in error.call_args.args[0]

    def test_staging_path_mismatch_aborts_before_download(self, fake_ydl, tmp_path):
        class _StagingIgnoredYDL(_FakeYDL):
            def prepare_filename(self, info: dict) -> str:
                return self.opts["outtmpl"].replace("%(ext)s", info["ext"])

        final = _existing_final(tmp_path)
        with patch("yt_dlp.YoutubeDL", _StagingIgnoredYDL), pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD

    def test_missing_staged_file_after_success_is_a_failure(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        with pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD

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
            patch.object(download.progress, "error") as error,
            pytest.raises(DownloadFailedError),
        ):
            _overwrite(tmp_path)
        assert fake_ydl.instances[-1].final_path is None
        assert final.read_bytes() == _OLD
        assert (tmp_path / locked).read_bytes() == b"stale staging"
        assert locked in error.call_args.args[0]

    def test_cleanup_after_failed_attempt_stays_best_effort(self, fake_ydl, tmp_path):
        final = _existing_final(tmp_path)
        fake_ydl.payload = _NEW
        fake_ydl.fail_in = "process_ie_result"
        with _unlink_fails_for("Show - ep1.redownload.mp4.part"), pytest.raises(DownloadFailedError):
            _overwrite(tmp_path)
        assert final.read_bytes() == _OLD
