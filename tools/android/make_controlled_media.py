"""Regenerates the controlled server's media (needs ffmpeg); the output is committed."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(__file__).parent / "media"
DURATION_SEC = 30
SEGMENT_SEC = 2
# bit-exact single-threaded x264 so a rerun gives identical files
ENCODE = [
    "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=15:duration={DURATION_SEC}",
    "-c:v", "libx264", "-profile:v", "baseline", "-preset", "veryfast", "-b:v", "48k", "-x264-params", "threads=1",
    "-g", str(15 * SEGMENT_SEC), "-keyint_min", str(15 * SEGMENT_SEC), "-sc_threshold", "0", "-pix_fmt", "yuv420p",
    "-an", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
]  # fmt: skip


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, cwd=cwd)


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is not on PATH", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        source = staging / "source.mp4"
        run(*ENCODE, str(source))
        run("-i", str(source), "-c", "copy", "-movflags", "+faststart", "-fflags", "+bitexact", str(staging / "video.mp4"))
        (staging / "hls").mkdir()
        run(
            "-i", str(source), "-c", "copy", "-f", "hls", "-hls_time", str(SEGMENT_SEC), "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(staging / "hls" / "seg_%03d.ts"), str(staging / "hls" / "index.m3u8"),
        )  # fmt: skip
        (staging / "dash").mkdir()
        run(
            "-i", str(source), "-c", "copy", "-f", "dash", "-seg_duration", str(SEGMENT_SEC), "-use_template", "1",
            "-use_timeline", "0", "-init_seg_name", "init.m4s", "-media_seg_name", "chunk_$Number%03d$.m4s",
            "manifest.mpd",
            # the DASH muxer writes segments next to the working directory, not next to the manifest
            cwd=staging / "dash",
        )  # fmt: skip
        source.unlink()
        shutil.rmtree(OUT, ignore_errors=True)
        shutil.copytree(staging, OUT)
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"wrote {OUT} ({total} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
