"""Stage 2 — Transcribe (local Whisper).

Uses faster-whisper (CTranslate2) by default: 4x faster and lower memory than
the reference implementation, with the same accuracy. Returns a structured
transcript with **word-level** timestamps — these drive both clip boundary
snapping and karaoke subtitle timing downstream.

Performance: the model is loaded once per worker process and cached (module
singleton keyed by model+device+compute_type), not reloaded on every task —
loading `large-v3` alone can take 10-60s, which used to happen on every
single video. `WHISPER_DEVICE=auto` probes for a usable CUDA device and falls
back to CPU automatically (see `app.core.gpu`) rather than crashing when a
worker without a GPU inherits a `cuda` setting meant for a different host.

The backend is pluggable via WHISPER_BACKEND so a future GPU/whisperX/Deepgram
backend can be dropped in without touching callers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import CorruptedMediaError, TranscriptionError
from app.core.gpu import resolve_whisper_device
from app.core.logging import logger
from app.pipeline.ffmpeg_utils import extract_audio, get_duration

ProgressCb = Callable[[float, str], None]

_model_cache: dict[tuple[str, str, str], object] = {}


def _get_model(model_name: str, device: str, compute_type: str):
    """Return a cached WhisperModel for this (model, device, compute_type)
    triple, loading it at most once per worker process."""
    key = (model_name, device, compute_type)
    if not settings.whisper_cache_models:
        from faster_whisper import WhisperModel

        return WhisperModel(model_name, device=device, compute_type=compute_type)

    cached = _model_cache.get(key)
    if cached is None:
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper model={} device={} compute={} (first use this process)",
            model_name,
            device,
            compute_type,
        )
        try:
            cached = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as e:
            raise TranscriptionError(
                f"Failed to load Whisper model '{model_name}' on {device}: {e}"
            ) from e
        _model_cache[key] = cached
    return cached


def transcribe(
    source_path: str | Path, work_dir: Path, on_progress: ProgressCb | None = None
) -> dict:
    """Return {"language", "duration", "segments": [{start,end,text,words:[…]}]}."""
    try:
        audio = extract_audio(source_path, work_dir / "audio.wav")
    except Exception as e:
        raise TranscriptionError(f"Failed to extract audio from {source_path}: {e}") from e

    if audio.stat().st_size == 0:
        raise CorruptedMediaError(f"Extracted audio from {source_path} is empty")

    if settings.whisper_backend == "faster_whisper":
        return _faster_whisper(audio, on_progress)
    raise TranscriptionError(f"Unknown WHISPER_BACKEND: {settings.whisper_backend}")


def _faster_whisper(audio: Path, on_progress: ProgressCb | None) -> dict:
    device = resolve_whisper_device(settings.whisper_device)
    # int8 is CPU-friendly; float16 needs CUDA. If we resolved to CPU but the
    # configured compute type assumes a GPU, fall back to a CPU-safe default
    # rather than erroring deep inside ctranslate2.
    compute_type = settings.whisper_compute_type
    if device == "cpu" and compute_type in ("float16",):
        logger.warning("compute_type={} needs CUDA; using int8 on CPU instead", compute_type)
        compute_type = "int8"

    model = _get_model(settings.whisper_model, device, compute_type)

    try:
        total = get_duration(audio)
    except Exception as e:
        raise CorruptedMediaError(f"Could not read duration of extracted audio: {e}") from e

    try:
        seg_iter, info = model.transcribe(
            str(audio),
            word_timestamps=True,  # required for karaoke subtitles + snapping
            vad_filter=True,  # drop silence -> cleaner segments
            beam_size=5,
        )

        segments: list[dict] = []
        for seg in seg_iter:
            words = [{"start": w.start, "end": w.end, "word": w.word} for w in (seg.words or [])]
            segments.append(
                {"start": seg.start, "end": seg.end, "text": seg.text.strip(), "words": words}
            )
            if on_progress and total:
                try:
                    on_progress(min(seg.end / total, 0.99), f"Transcribed {seg.end:.0f}s")
                except Exception as e:
                    # A progress callback is a nice-to-have (usually a DB
                    # write); a transient failure here must not abort a
                    # 20-minute transcription that is otherwise succeeding.
                    logger.warning("Progress callback failed (continuing): {}", e)
    except Exception as e:
        raise TranscriptionError(f"faster-whisper transcription failed: {e}") from e

    if not segments:
        raise TranscriptionError("Transcription produced zero segments — check audio content")

    if on_progress:
        try:
            on_progress(1.0, "Transcription complete")
        except Exception as e:
            logger.warning("Final progress callback failed: {}", e)

    logger.info("Transcribed {} segments ({})", len(segments), info.language)
    return {
        "language": info.language,
        "duration": total,
        "segments": segments,
    }


def full_text(transcript: dict) -> str:
    return " ".join(s["text"] for s in transcript["segments"])
