"""Thin, typed wrappers around the ffmpeg/ffprobe binaries.

We shell out rather than use a Python binding: FFmpeg's CLI is the stable,
well-documented interface, and it keeps heavy media work out of the Python
GIL. Every invocation is:
  - time-bounded (FFMPEG_TIMEOUT_SECONDS) so a stuck process can't hold a
    worker slot indefinitely,
  - logged at debug level with the full command and at error level with the
    tail of stderr on failure,
  - quiet by default (`-loglevel error`) to keep captured output small.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import FFmpegExecutionError, FFmpegTimeoutError
from app.core.gpu import ffmpeg_hwaccel_encoder
from app.core.logging import logger

# Kept for backwards compatibility with any external code importing this name.
FFmpegError = FFmpegExecutionError


def run(cmd: list[str], *, timeout: int | None = None) -> str:
    """Run a command, raising FFmpegExecutionError with stderr on failure or
    FFmpegTimeoutError if it exceeds the timeout."""
    timeout = timeout or settings.ffmpeg_timeout_seconds
    logger.debug("exec: {}", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise FFmpegTimeoutError(
            f"Command exceeded {timeout}s timeout: {' '.join(cmd[:4])}..."
        ) from e
    except OSError as e:
        raise FFmpegExecutionError(f"Failed to execute {cmd[0]}: {e}") from e

    if proc.returncode != 0:
        raise FFmpegExecutionError(
            f"Command failed ({proc.returncode}): {' '.join(cmd[:4])}...\n" f"{proc.stderr[-2000:]}"
        )
    return proc.stdout


def probe(path: str | Path) -> dict:
    """Return the ffprobe JSON for a media file."""
    out = run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60,
    )
    return json.loads(out)


def get_duration(path: str | Path) -> float:
    info = probe(path)
    return float(info["format"]["duration"])


def get_video_dimensions(path: str | Path) -> tuple[int, int]:
    info = probe(path)
    for stream in info["streams"]:
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise FFmpegExecutionError(f"No video stream in {path}")


def extract_audio(src: str | Path, dst: str | Path, sample_rate: int = 16000) -> Path:
    """Extract mono 16 kHz WAV — the format Whisper expects."""
    dst = Path(dst)
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    return dst


#: Number of tokens each encoder's `-c:v ...` block occupies in the list
#: `_video_encoder_args()` returns — e.g. h264_nvenc's block is
#: `-c:v h264_nvenc -preset p4 -cq N` (6 tokens). `_run_encode`'s libx264
#: fallback needs this to splice out exactly that span; slicing out only
#: `-c:v <encoder>` (2 tokens) left the rest of the encoder's flags — e.g.
#: `-cq 20` — behind as stray positional args, which ffmpeg then
#: misinterpreted as an extra output filename (`20`) and failed on.
_ENCODER_ARG_SPAN = {"h264_nvenc": 6, "h264_vaapi": 2, "h264_qsv": 2}


def _video_encoder_args() -> list[str]:
    """Pick the fastest available encoder. Falls back to libx264 both when
    FFMPEG_HWACCEL=none and when hardware acceleration was requested but
    isn't actually available/working (the caller retries once in that case
    via `encode_with_fallback`)."""
    encoder = ffmpeg_hwaccel_encoder(settings.ffmpeg_hwaccel)
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(settings.ffmpeg_crf)]
    if encoder in ("h264_vaapi", "h264_qsv"):
        return ["-c:v", encoder]
    return ["-c:v", "libx264", "-preset", settings.ffmpeg_preset, "-crf", str(settings.ffmpeg_crf)]


def _run_encode(cmd: list[str]) -> str:
    """Run an encode command; if a hardware encoder was used and fails,
    transparently retry once with libx264 rather than failing the whole clip
    over a flaky/unavailable GPU encoder."""
    try:
        return run(cmd)
    except FFmpegExecutionError:
        if "-c:v" not in cmd:
            raise
        idx = cmd.index("-c:v")
        encoder = cmd[idx + 1]
        if encoder == "libx264":
            raise
        logger.warning("Hardware encoder failed, retrying with libx264: {}", encoder)
        span = _ENCODER_ARG_SPAN.get(encoder, 2)
        fallback = (
            cmd[:idx]
            + ["-c:v", "libx264", "-preset", settings.ffmpeg_preset, "-crf", str(settings.ffmpeg_crf)]
            + cmd[idx + span :]
        )
        return run(fallback)


def cut_segment(src: str | Path, dst: str | Path, start: float, end: float) -> Path:
    """Accurately cut [start, end] by re-encoding (frame-accurate, no keyframe
    drift). Input seeking (`-ss` before `-i`) keeps it fast."""
    dst = Path(dst)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(src),
        *_video_encoder_args(),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-avoid_negative_ts",
        "make_zero",
        str(dst),
    ]
    _run_encode(cmd)
    return dst


def mux_video_audio(video_only: str | Path, audio_source: str | Path, dst: str | Path) -> Path:
    dst = Path(dst)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_only),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        *_video_encoder_args(),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        str(dst),
    ]
    _run_encode(cmd)
    return dst


def burn_subtitles(clip_path: str | Path, ass_path: Path, dst: str | Path) -> Path:
    dst = Path(dst)
    escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(clip_path),
        "-vf",
        f"subtitles='{escaped}'",
        *_video_encoder_args(),
        "-c:a",
        "copy",
        str(dst),
    ]
    _run_encode(cmd)
    return dst


def make_thumbnail(src: str | Path, dst: str | Path, at: float = 0.5) -> Path:
    dst = Path(dst)
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dst),
        ],
        timeout=60,
    )
    return dst
