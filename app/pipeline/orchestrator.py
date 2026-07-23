"""Orchestrator — chains the stages and persists state as it goes.

Two entry points, both called from Celery tasks:

    prepare_video(video_id)   ingest -> transcribe -> analyze -> create Clip rows
    render_clip(clip_id)      cut -> reframe -> subtitles -> metadata -> package

Splitting "prepare" from "render" lets clips render in parallel across workers
and lets a single failed clip be retried without re-running Whisper/Claude.

Every stage is timed (Prometheus histogram) and wrapped so a stage-specific
exception (from `app.core.exceptions`) is what callers see — Celery uses
`.retryable` on these to decide whether to retry or give up immediately.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from slugify import slugify
from sqlalchemy.orm import Session

from app.core.cleanup import cleanup_clip_workspace_on_failure, cleanup_video_workspace
from app.core.config import settings
from app.core.exceptions import ClipFarmError, DuplicateJobError
from app.core.logging import logger
from app.core.metrics import CLIPS_RENDERED, VIDEOS_IN_FLIGHT, StageTimer
from app.models import Clip, ClipStatus, Job, JobStatus, Video
from app.pipeline import analyze, cut, ingest, metadata, reframe, subtitles, transcribe
from app.pipeline.ffmpeg_utils import make_thumbnail


# --- Job bookkeeping helpers --------------------------------------------------

def _job(db: Session, video_id: str, stage: str) -> Job:
    job = Job(video_id=video_id, stage=stage, status=JobStatus.running)
    db.add(job)
    db.commit()
    return job


def _progress(db: Session, job: Job, p: float, msg: str) -> None:
    job.progress = p
    job.message = msg
    db.commit()


def _finish(db: Session, job: Job, ok: bool, err: str | None = None) -> None:
    job.status = JobStatus.completed if ok else JobStatus.failed
    job.progress = 1.0 if ok else job.progress
    job.error = err[:4000] if err else None
    db.commit()


def _fail_open_job(job: Job | None, err: str) -> None:
    """Mark whichever stage `Job` row was in progress when an exception
    propagated out of `prepare_video` as failed, rather than leaving it
    stuck at `running` forever. Without this, a stage failure correctly
    fails the video but its Job row silently vanishes from the
    `clipfarm.failed_job_report` / `/api/jobs/failed` view — the failure
    still happened, monitoring just never saw it. The caller commits
    alongside the video's own status update."""
    if job is not None and job.status == JobStatus.running:
        job.status = JobStatus.failed
        job.error = err[:4000]


# --- Stage 1-3: prepare -------------------------------------------------------

def prepare_video(db: Session, video_id: str) -> list[str]:
    """Ingest, transcribe and analyze. Returns the created clip IDs."""
    video = db.get(Video, video_id)
    if video is None:
        raise ValueError(f"Video {video_id} not found")

    if video.status == JobStatus.running:
        raise DuplicateJobError(
            f"Video {video_id} is already being processed — refusing to start a "
            "second concurrent run"
        )

    video.status = JobStatus.running
    video.error = None
    db.commit()
    VIDEOS_IN_FLIGHT.inc()
    job: Job | None = None

    try:
        # 1. Ingest
        job = _job(db, video_id, "ingest")
        with StageTimer("ingest"):
            result = ingest.ingest(video_id, video.source_type, video.source_ref)
        video.source_path = str(result.path)
        video.title = video.title or result.title
        video.duration_seconds = result.duration
        db.commit()
        _finish(db, job, True)

        # 2. Transcribe
        job = _job(db, video_id, "transcribe")
        with StageTimer("transcribe"):
            transcript = transcribe.transcribe(
                result.path,
                settings.work_dir / video_id,
                on_progress=lambda p, m: _progress(db, job, p, m),
            )
        video.transcript = transcript
        db.commit()
        _finish(db, job, True)

        # 3. Analyze -> Clip rows
        job = _job(db, video_id, "analyze")
        with StageTimer("analyze"):
            candidates = analyze.analyze(transcript)
        clip_ids: list[str] = []
        for rank, c in enumerate(candidates, start=1):
            clip = Clip(
                video_id=video_id,
                rank=rank,
                start_seconds=c.start_seconds,
                end_seconds=c.end_seconds,
                score=c.score,
                reason=c.reason,
                categories=c.categories,
                transcript_text=c.transcript_text,
                status=ClipStatus.selected,
            )
            db.add(clip)
            db.flush()
            clip_ids.append(clip.id)
        db.commit()
        _finish(db, job, True)

        logger.info("Prepared video {} -> {} clips", video_id, len(clip_ids))
        return clip_ids

    except ClipFarmError as e:
        logger.error("prepare_video({}) failed: {}", video_id, e)
        video.status = JobStatus.failed
        video.error = str(e)[:4000]
        _fail_open_job(job, str(e))
        db.commit()
        raise
    except Exception as e:  # noqa: BLE001 - unexpected bug, still record it
        logger.exception("prepare_video({}) failed unexpectedly", video_id)
        video.status = JobStatus.failed
        video.error = f"Unexpected error: {e}"[:4000]
        _fail_open_job(job, f"Unexpected error: {e}")
        db.commit()
        raise
    finally:
        VIDEOS_IN_FLIGHT.dec()


# --- Stage 4-7: render one clip ----------------------------------------------

def render_clip(db: Session, clip_id: str) -> str:
    """Cut, reframe, subtitle and package one clip. Returns the output path."""
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise ValueError(f"Clip {clip_id} not found")
    video = db.get(Video, clip.video_id)
    if video is None or not video.source_path:
        raise ValueError(f"Video {clip.video_id} has no ingested source")

    clip.status = ClipStatus.rendering
    clip.render_started_at = datetime.utcnow()
    clip.error = None
    db.commit()

    work = settings.work_dir / clip.video_id / "clips" / clip.id
    work.mkdir(parents=True, exist_ok=True)

    try:
        # 4. Cut segment
        with StageTimer("cut"):
            raw = cut.cut(
                video.source_path, work / "cut.mp4", clip.start_seconds, clip.end_seconds
            )
        # 5. Reframe to 9:16 with speaker tracking
        with StageTimer("reframe"):
            framed = reframe.reframe(raw, work / "framed.mp4", work)
        # 6. Animated subtitles
        with StageTimer("subtitles"):
            ass = subtitles.build_ass(
                video.transcript, clip.start_seconds, clip.end_seconds, work / "subs.ass"
            )
        # 7. Metadata (Claude)
        with StageTimer("metadata"):
            meta = metadata.generate(clip.transcript_text, context_title=video.title)

        clip.gen_title = meta.title
        clip.gen_hook = meta.hook
        clip.gen_description = meta.description
        clip.gen_hashtags = meta.hashtags

        # --- Package into an organised output folder ---
        out_dir = (
            settings.output_dir
            / _safe(video.title or video.id)
            / f"{clip.rank:02d}_{_safe(meta.title)[:48]}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        final = out_dir / "clip.mp4"
        with StageTimer("burn_subtitles"):
            subtitles.burn(framed, ass, final)
        thumb = make_thumbnail(final, out_dir / "thumbnail.jpg")

        (out_dir / "metadata.json").write_text(
            json.dumps(_metadata_dict(clip, meta), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        clip.output_path = str(final)
        clip.thumbnail_path = str(thumb)
        clip.status = ClipStatus.completed
        clip.render_finished_at = datetime.utcnow()
        db.commit()

        # Per-clip intermediates (cut.mp4/framed.mp4/subs.ass) are no longer
        # needed on success — the packaged output already lives under
        # output_dir, so always reclaim them (unlike the failure path, this
        # is unconditional — KEEP_WORK_DIR_ON_FAILURE only applies to failures).
        _rmtree_quiet(work)

        CLIPS_RENDERED.labels(outcome="success").inc()
        logger.info("Rendered clip {} -> {}", clip.id, final)
        return str(final)

    except ClipFarmError as e:
        logger.error("render_clip({}) failed: {}", clip_id, e)
        clip.status = ClipStatus.failed
        clip.error = str(e)[:4000]
        clip.render_finished_at = datetime.utcnow()
        db.commit()
        cleanup_clip_workspace_on_failure(clip.video_id, clip.id)
        CLIPS_RENDERED.labels(outcome="failure").inc()
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("render_clip({}) failed unexpectedly", clip_id)
        clip.status = ClipStatus.failed
        clip.error = f"Unexpected error: {e}"[:4000]
        clip.render_finished_at = datetime.utcnow()
        db.commit()
        cleanup_clip_workspace_on_failure(clip.video_id, clip.id)
        CLIPS_RENDERED.labels(outcome="failure").inc()
        raise


def finalize_video(db: Session, video_id: str) -> None:
    """Mark the video completed once all its clips have been attempted, and
    reclaim the shared per-video workspace (source download, extracted
    audio) now that every clip has either rendered or permanently failed."""
    video = db.get(Video, video_id)
    if video is None:
        return

    failed = sum(1 for c in video.clips if c.status == ClipStatus.failed)
    total = len(video.clips)
    video.status = JobStatus.completed
    if failed and failed == total and total > 0:
        video.error = f"All {total} clips failed to render"
    elif failed:
        video.error = f"{failed}/{total} clips failed to render (rest succeeded)"
    db.commit()

    cleanup_video_workspace(video_id)


# --- helpers ------------------------------------------------------------------

def _safe(name: str) -> str:
    return slugify(name, max_length=64) or "untitled"


def _rmtree_quiet(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _metadata_dict(clip: Clip, meta) -> dict:
    return {
        "rank": clip.rank,
        "score": clip.score,
        "duration_seconds": clip.duration,
        "categories": clip.categories,
        "why_selected": clip.reason,
        "title": meta.title,
        "hook": meta.hook,
        "description": meta.description,
        "hashtags": meta.hashtags,
        "transcript": clip.transcript_text,
    }
