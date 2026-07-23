"""GPU / hardware-acceleration detection.

Centralises "is there a usable NVIDIA GPU" logic so Whisper and FFmpeg agree
on what hardware is actually available, instead of each guessing separately
or trusting an operator-set env var that's gone stale (e.g. after a host
migration from a GPU box to a CPU-only box).

Detection is cheap (subprocess + optional torch/ctranslate2 probe) and cached
for the process lifetime — call `gpu_available()` freely.
"""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache

from app.core.logging import logger


@lru_cache
def nvidia_smi_available() -> bool:
    """True if `nvidia-smi` runs successfully (driver + GPU present)."""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        proc = subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5
        )
        return proc.returncode == 0 and "GPU" in proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


@lru_cache
def ctranslate2_cuda_available() -> bool:
    """True if the ctranslate2 build faster-whisper uses can see a CUDA device.

    This is the authoritative check for Whisper specifically — nvidia-smi can
    be present while the installed ctranslate2 wheel is CPU-only.
    """
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - any import/probe failure means "no"
        return False


@lru_cache
def resolve_whisper_device(configured: str) -> str:
    """Resolve WHISPER_DEVICE=auto to a concrete device, falling back to CPU
    with a clear log line if CUDA was requested/auto but isn't actually usable."""
    if configured == "cpu":
        return "cpu"
    if configured in ("cuda", "auto"):
        if ctranslate2_cuda_available():
            return "cuda"
        if configured == "cuda":
            logger.warning(
                "WHISPER_DEVICE=cuda requested but no usable CUDA device found — "
                "falling back to CPU. Transcription will be slower."
            )
        return "cpu"
    return configured


@lru_cache
def ffmpeg_hwaccel_encoder(configured: str) -> str | None:
    """Return the ffmpeg video encoder name for hardware acceleration, or
    None to use software (libx264). `configured` is FFMPEG_HWACCEL from
    settings: auto | none | nvenc | vaapi | qsv."""
    if configured == "none":
        return None
    if configured == "nvenc":
        return "h264_nvenc" if nvidia_smi_available() else None
    if configured in ("vaapi", "qsv"):
        # VAAPI/QSV need device nodes (/dev/dri) mounted into the container;
        # we only claim support if explicitly requested since there's no
        # cheap universal probe — the caller falls back to libx264 on error.
        return {"vaapi": "h264_vaapi", "qsv": "h264_qsv"}[configured]
    if configured == "auto":
        if nvidia_smi_available():
            return "h264_nvenc"
        return None
    return None


def describe_hardware() -> dict:
    """Human-readable hardware summary, used by /health/ready and startup logs."""
    return {
        "nvidia_gpu_present": nvidia_smi_available(),
        "whisper_cuda_available": ctranslate2_cuda_available(),
    }
