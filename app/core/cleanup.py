"""Disk retention helpers.

Two mechanisms work together:
  1. `cleanup_video_workspace` — called by the orchestrator right after a
     video's clips finish rendering (success path). Deletes the per-video
     working directory (source download, extracted audio, per-clip
     intermediates) since everything a user needs lives in `output_dir`.
  2. `purge_stale_work_dirs` — a Celery Beat safety net that walks
     `WORK_DIR` and removes anything older than `WORK_DIR_RETENTION_HOURS`,
     regardless of why the normal cleanup path was skipped (crash, manual
     kill -9, `KEEP_WORK_DIR_ON_FAILURE=true` debugging session that was
     never followed up on).

Both are defensive: a failure to delete (permission error, file still open on
Windows-mounted volumes, etc.) is logged and swallowed — cleanup must never
be the reason a pipeline run fails.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from pathlib import Path

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import WORK_DIR_BYTES


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def cleanup_video_workspace(video_id: str) -> None:
    """Remove `WORK_DIR/<video_id>` entirely. Safe to call multiple times."""
    if not settings.cleanup_work_dir_on_success:
        return
    work = settings.work_dir / video_id
    if not work.exists():
        return
    try:
        shutil.rmtree(work, ignore_errors=False)
        logger.debug("Cleaned up workspace for video {}", video_id)
    except OSError as e:
        logger.warning("Failed to clean up workspace {}: {}", work, e)


def cleanup_clip_workspace_on_failure(video_id: str, clip_id: str) -> None:
    """On a failed render, remove the per-clip temp dir unless the operator
    opted to keep failures around for debugging."""
    if settings.keep_work_dir_on_failure:
        return
    clip_work = settings.work_dir / video_id / "clips" / clip_id
    if clip_work.exists():
        shutil.rmtree(clip_work, ignore_errors=True)


def purge_stale_work_dirs() -> dict:
    """Delete any top-level entry under WORK_DIR older than the configured
    retention window. Returns a small report for logging/alerting."""
    cutoff = time.time() - settings.work_dir_retention_hours * 3600
    removed: list[str] = []
    errors: list[str] = []

    if not settings.work_dir.exists():
        return {"removed": removed, "errors": errors}

    for entry in settings.work_dir.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=False)
                else:
                    entry.unlink()
                removed.append(entry.name)
        except OSError as e:
            errors.append(f"{entry.name}: {e}")

    if removed or errors:
        logger.info(
            "Stale work-dir purge: removed {} entries, {} errors", len(removed), len(errors)
        )

    with contextlib.suppress(OSError):
        WORK_DIR_BYTES.set(_dir_size_bytes(settings.work_dir))

    return {"removed": removed, "errors": errors}
