"""Input validation shared by the API layer and the ingest pipeline stage.

Every function here raises `app.core.exceptions.ValidationError` (or a
subclass) on bad input — never a bare `ValueError`/`AssertionError` — so
callers can catch one type and turn it into a clean 400 response.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from slugify import slugify

from app.core.config import settings
from app.core.exceptions import (
    CorruptedMediaError,
    FileTooLargeError,
    UnsupportedSourceError,
    ValidationError,
)

# Loosely matches http(s) URLs — full RFC 3986 validation is unnecessary here;
# yt-dlp itself will reject anything it can't extract.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def validate_youtube_url(url: str) -> str:
    """Validate a submitted source_ref is an http(s) URL on an allow-listed
    host. This is the primary SSRF guard: without it, yt-dlp (which supports
    hundreds of extractors, including generic HTTP/HLS) could be pointed at
    an internal service, a cloud metadata endpoint, or a `file://` URI.
    """
    url = url.strip()
    if not url or not _URL_RE.match(url):
        raise ValidationError("source_ref must be an http:// or https:// URL")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationError("Could not parse a hostname from source_ref")

    allowed = settings.allowed_source_host_set
    if not any(host == h or host.endswith(f".{h}") for h in allowed):
        raise UnsupportedSourceError(
            f"Host '{host}' is not in ALLOWED_SOURCE_HOSTS ({', '.join(sorted(allowed))})"
        )
    return url


def sanitize_filename(filename: str) -> str:
    """Turn an arbitrary client-supplied filename into a safe basename.

    Prevents path traversal (`../../etc/passwd`, absolute paths, embedded
    separators) by discarding all directory components and re-slugifying the
    stem while preserving the extension.
    """
    if not filename:
        raise ValidationError("Filename is required")

    # Path(...).name strips any directory components (Windows and POSIX
    # separators alike) — this alone kills `../` traversal attempts.
    base = Path(filename.replace("\\", "/")).name
    stem, _, ext = base.rpartition(".")
    if not stem:
        stem, ext = ext, ""
    ext = f".{ext.lower()}" if ext else ""

    if ext not in settings.allowed_video_extension_set:
        raise UnsupportedSourceError(
            f"Extension '{ext or '(none)'}' not allowed "
            f"({', '.join(sorted(settings.allowed_video_extension_set))})"
        )

    safe_stem = slugify(stem, max_length=100) or "upload"
    return f"{safe_stem}{ext}"


def validate_upload_size(size_bytes: int) -> None:
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if size_bytes <= 0:
        raise ValidationError("Uploaded file is empty")
    if size_bytes > max_bytes:
        raise FileTooLargeError(
            f"File is {size_bytes / 1024 / 1024:.1f} MB, "
            f"limit is {settings.max_upload_size_mb} MB"
        )


def resolve_local_source(ref: str) -> Path:
    """Resolve a `source_ref` for local/uploaded videos to a path *strictly
    inside* INPUT_DIR. Rejects absolute paths and any traversal outside the
    sandbox — without this, a caller could set source_ref to an arbitrary
    filesystem path the worker process can read.
    """
    candidate = (settings.input_dir / Path(ref).name).resolve()
    input_root = settings.input_dir.resolve()
    if input_root not in candidate.parents and candidate != input_root:
        raise ValidationError("source_ref resolves outside the input directory")
    if not candidate.exists():
        raise ValidationError(f"File not found in input directory: {ref}")
    return candidate


def validate_media_file(path: Path, *, min_duration: float = 0.5) -> dict:
    """ffprobe-based corrupted/empty file detection. Returns the probe dict on
    success. Call this right after any ingest (download, copy, upload) before
    handing the file to the rest of the pipeline — catching a truncated
    download here is far cheaper than discovering it mid-transcription.
    """
    from app.pipeline.ffmpeg_utils import FFmpegError, get_duration, probe

    if not path.exists() or path.stat().st_size == 0:
        raise CorruptedMediaError(f"{path} does not exist or is empty")

    try:
        info = probe(path)
    except FFmpegError as e:
        raise CorruptedMediaError(f"ffprobe could not read {path.name}: {e}") from e

    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise CorruptedMediaError(f"{path.name} has no video stream")

    try:
        duration = get_duration(path)
    except (FFmpegError, KeyError, ValueError) as e:
        raise CorruptedMediaError(f"{path.name} has no readable duration: {e}") from e

    if duration < min_duration:
        raise CorruptedMediaError(
            f"{path.name} duration is {duration:.2f}s (< {min_duration}s minimum) — "
            "likely a truncated or failed download"
        )
    return info
