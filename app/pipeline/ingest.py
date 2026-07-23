"""Stage 1 — Ingest.

Turn a YouTube URL or a local/uploaded MP4 into a normalised local file plus
basic metadata. YouTube uses yt-dlp; local files are copied into the work dir.

Every source is validated before touching yt-dlp/ffmpeg:
  - YouTube URLs must be http(s) on an allow-listed host (SSRF guard — see
    `app.core.validation.validate_youtube_url`; without this, yt-dlp's dozens
    of generic extractors could be pointed at an internal service).
  - Local/uploaded refs are resolved strictly inside INPUT_DIR (path-traversal
    guard — see `resolve_local_source`).
  - The resulting file is probed with ffprobe before we call it "ingested",
    catching truncated downloads and corrupted uploads immediately instead of
    deep inside a 20-minute transcription run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from app.core.config import settings
from app.core.exceptions import IngestError
from app.core.logging import logger
from app.core.validation import resolve_local_source, validate_media_file, validate_youtube_url
from app.models import SourceType
from app.pipeline.ffmpeg_utils import get_duration


@dataclass
class IngestResult:
    path: Path
    title: str
    duration: float


def ingest(video_id: str, source_type: SourceType, source_ref: str) -> IngestResult:
    work = settings.work_dir / video_id
    work.mkdir(parents=True, exist_ok=True)

    if source_type == SourceType.youtube:
        result = _ingest_youtube(source_ref, work)
    else:
        result = _ingest_local(source_ref, work)

    validate_media_file(result.path)
    return result


def _ingest_youtube(url: str, work: Path) -> IngestResult:
    url = validate_youtube_url(url)
    logger.info("Downloading YouTube video: {}", url)
    out_tmpl = str(work / "source.%(ext)s")
    max_bytes = settings.max_download_size_mb * 1024 * 1024
    ydl_opts = {
        # Prefer <=1080p mp4 to keep re-framing fast and files sane.
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": max_bytes,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise IngestError(f"yt-dlp failed to download {url}: {e}") from e

    if info is None:
        raise IngestError(f"yt-dlp returned no metadata for {url}")

    path = work / "source.mp4"
    if not path.exists():  # yt-dlp may keep the original container ext
        candidates = list(work.glob("source.*"))
        if not candidates:
            raise IngestError("yt-dlp produced no output file")
        path = candidates[0]

    try:
        duration = float(info.get("duration") or get_duration(path))
    except (TypeError, ValueError, KeyError) as e:
        raise IngestError(f"Could not determine duration for {url}: {e}") from e

    return IngestResult(path=path, title=info.get("title") or "Untitled", duration=duration)


def _ingest_local(ref: str, work: Path) -> IngestResult:
    src = resolve_local_source(ref)
    dst = work / f"source{src.suffix or '.mp4'}"
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        raise IngestError(f"Failed to copy local source {src}: {e}") from e
    logger.info("Copied local source {} -> {}", src, dst)
    return IngestResult(path=dst, title=src.stem, duration=get_duration(dst))
