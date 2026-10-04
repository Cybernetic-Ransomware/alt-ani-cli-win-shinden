from pathlib import Path
from unittest.mock import patch

import pytest

from alt_ani_cli import download
from alt_ani_cli.config import USER_AGENT
from alt_ani_cli.extract.common import Stream
from alt_ani_cli.models import EpisodeRow, SeriesRef

_EP = EpisodeRow(number=1, title="Ep", url="https://shinden.pl/episode/1")
_SERIES = SeriesRef(id="1", slug="show", title="Show", url="https://shinden.pl/series/1-show")


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records opts and the on-disk state seen when the download starts."""

    instances: list[_FakeYDL] = []
    info: dict = {}

    def __init__(self, opts: dict) -> None:
        self.opts = opts
        self.final_path: Path | None = None
        self.part_existed_at_download: bool | None = None
        _FakeYDL.instances.append(self)

    def __enter__(self) -> _FakeYDL:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict:
        assert download is False
        return dict(self.info)

    def prepare_filename(self, info: dict) -> str:
        return self.opts["outtmpl"].replace("%(ext)s", info["ext"])

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        self.final_path = Path(self.prepare_filename(info))
        self.part_existed_at_download = self.final_path.with_name(self.final_path.name + ".part").exists()
        return info


@pytest.fixture
def fake_ydl():
    _FakeYDL.instances = []
    _FakeYDL.info = {"ext": "mp4", "protocol": "m3u8_native"}
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
