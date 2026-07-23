"""Custom exception hierarchy.

Every exception the pipeline can raise on purpose (as opposed to a genuine
bug) derives from `ClipFarmError`. This lets callers (Celery tasks, the API)
distinguish "this input/environment is bad, don't blindly retry" from
transient errors that *should* retry, without string-matching messages.
"""
from __future__ import annotations


class ClipFarmError(Exception):
    """Base class for all deliberately-raised application errors."""

    #: Whether a Celery task should retry this error. Subclasses override.
    retryable: bool = False


# --- Validation / input errors (never retryable — the input itself is bad) --

class ValidationError(ClipFarmError):
    """Bad user input (URL, filename, size, unsupported format, ...)."""

    retryable = False


class UnsupportedSourceError(ValidationError):
    pass


class FileTooLargeError(ValidationError):
    pass


class CorruptedMediaError(ValidationError):
    """ffprobe couldn't find a usable video/audio stream, or duration is 0."""

    retryable = False


# --- Pipeline stage errors ----------------------------------------------------

class IngestError(ClipFarmError):
    retryable = True   # network hiccups during download are common


class TranscriptionError(ClipFarmError):
    retryable = True


class AnalysisError(ClipFarmError):
    """Claude selection call failed after all retries."""

    retryable = True


class MetadataGenerationError(ClipFarmError):
    retryable = True


class RenderError(ClipFarmError):
    """Cut / reframe / subtitle burn failed."""

    retryable = True


# --- Infra errors --------------------------------------------------------------

class FFmpegExecutionError(ClipFarmError):
    retryable = True


class FFmpegTimeoutError(FFmpegExecutionError):
    """The ffmpeg/ffprobe process exceeded FFMPEG_TIMEOUT_SECONDS."""

    retryable = True


class ClaudeRefusalError(AnalysisError):
    """Claude declined the request for policy reasons. Retrying with the same
    prompt will not help — surface to a human instead of burning retries."""

    retryable = False


class ConfigurationError(ClipFarmError):
    """Missing/invalid configuration detected at runtime (not at import time,
    where a raised pydantic ValidationError is already appropriate)."""

    retryable = False


class DuplicateJobError(ClipFarmError):
    """A video/clip is already being processed — refuse to double-enqueue."""

    retryable = False
