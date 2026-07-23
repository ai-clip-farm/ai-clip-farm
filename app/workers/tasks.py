"""Celery tasks — the async backbone.

Flow:
    process_video          (prepare)  ── chord ──►  render_clip x N  ──►  finalize_video

`chord` fans clip rendering out across the worker pool and runs `finalize_video`
only after every clip finishes, so the video is marked complete exactly once.

Retry policy: only exceptions marked `.retryable = True` (see
`app.core.exceptions`) are retried — a bad YouTube URL or a Claude schema
mismatch will never succeed on retry, so those fail immediately instead of
burning 2-3 retry cycles (each potentially re-running a 20-minute
transcription) before giving up anyway.
"""

from __future__ import annotations

from celery import chord

from app.core.cleanup import purge_stale_work_dirs as _purge_stale_work_dirs
from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import ClipFarmError, DuplicateJobError
from app.core.logging import logger
from app.models import Clip, ClipStatus, JobStatus, Video
from app.pipeline import orchestrator
from app.workers.celery_app import celery_app


def _notify_slack(text: str) -> None:
    if not settings.slack_webhook_url:
        return
    try:
        import httpx

        httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=10)
    except Exception as e:
        logger.warning("Slack notification failed: {}", e)


@celery_app.task(bind=True, name="clipfarm.process_video", max_retries=3, default_retry_delay=60)
def process_video(self, video_id: str) -> dict:
    """Prepare a video, then dispatch a render task per clip."""
    try:
        with session_scope() as db:
            clip_ids = orchestrator.prepare_video(db, video_id)
    except DuplicateJobError as e:
        logger.warning(str(e))
        return {"video_id": video_id, "status": "skipped", "reason": str(e)}
    except ClipFarmError as e:
        if e.retryable and self.request.retries < self.max_retries:
            logger.warning(
                "process_video({}) transient failure, retry {}/{}: {}",
                video_id,
                self.request.retries + 1,
                self.max_retries,
                e,
            )
            raise self.retry(exc=e) from e
        _notify_slack(f":x: Video `{video_id}` permanently failed at prepare stage: {e}")
        return {"video_id": video_id, "status": "failed", "error": str(e)}

    if not clip_ids:
        with session_scope() as db:
            orchestrator.finalize_video(db, video_id)
        return {"video_id": video_id, "clips": 0}

    # Fan out rendering; finalize when all are done (success or fail).
    chord(
        (render_clip.s(cid) for cid in clip_ids),
        finalize_video.s(video_id),
    ).apply_async()
    return {"video_id": video_id, "clips": len(clip_ids)}


@celery_app.task(bind=True, name="clipfarm.render_clip", max_retries=2, default_retry_delay=30)
def render_clip(self, clip_id: str) -> dict:
    try:
        with session_scope() as db:
            path = orchestrator.render_clip(db, clip_id)
        return {"clip_id": clip_id, "status": "completed", "path": path}
    except ClipFarmError as e:
        logger.warning(
            "render_clip({}) failed (attempt {}/{}, retryable={}): {}",
            clip_id,
            self.request.retries,
            self.max_retries,
            e.retryable,
            e,
        )
        if e.retryable and self.request.retries < self.max_retries:
            with session_scope() as db:
                clip = db.get(Clip, clip_id)
                if clip is not None:
                    clip.retry_count = (clip.retry_count or 0) + 1
                    db.commit()
            raise self.retry(exc=e) from e
        return {"clip_id": clip_id, "status": "failed", "error": str(e)}
    except Exception as exc:
        logger.exception("render_clip({}) failed unexpectedly", clip_id)
        return {"clip_id": clip_id, "status": "failed", "error": str(exc)}


@celery_app.task(name="clipfarm.finalize_video")
def finalize_video(_results, video_id: str) -> dict:
    with session_scope() as db:
        orchestrator.finalize_video(db, video_id)
        video = db.get(Video, video_id)
        failed = sum(1 for c in video.clips if c.status == ClipStatus.failed) if video else 0
    if failed:
        _notify_slack(f":warning: Video `{video_id}` finished with {failed} failed clip(s).")
    logger.info("Video {} finalized ({} failed clips)", video_id, failed)
    return {"video_id": video_id, "status": "completed", "failed_clips": failed}


# --- Maintenance (Celery Beat) -------------------------------------------------


@celery_app.task(name="clipfarm.purge_stale_work_dirs")
def purge_stale_work_dirs() -> dict:
    """Safety-net disk cleanup — see app.core.cleanup for the retention rule."""
    return _purge_stale_work_dirs()


@celery_app.task(name="clipfarm.failed_job_report")
def failed_job_report() -> dict:
    """Summarize the last 24h of failures and (optionally) post to Slack.
    Also returned as JSON so it's inspectable via Flower / celery inspect."""
    import datetime as dt

    from sqlalchemy import select

    from app.models import Clip, Job

    # Naive UTC (not dt.datetime.now(dt.UTC) directly) to match the naive
    # DateTime columns (updated_at) it's compared against below.
    cutoff = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=24)
    with session_scope() as db:
        failed_videos = db.scalars(
            select(Video).where(Video.status == JobStatus.failed, Video.updated_at >= cutoff)
        ).all()
        failed_clips = db.scalars(
            select(Clip).where(Clip.status == ClipStatus.failed, Clip.updated_at >= cutoff)
        ).all()
        failed_jobs = db.scalars(
            select(Job).where(Job.status == JobStatus.failed, Job.updated_at >= cutoff)
        ).all()

        # Built as its own variable (not inlined into `report` below) so its
        # type is unambiguously dict[str, int] — folded into a heterogeneous
        # report dict, mypy can no longer prove that at the "counts" key.
        counts = {
            "videos": len(failed_videos),
            "clips": len(failed_clips),
            "stages": len(failed_jobs),
        }
        report = {
            "since": cutoff.isoformat(),
            "failed_videos": [
                {"id": v.id, "title": v.title, "error": v.error} for v in failed_videos
            ],
            "failed_clips": [
                {"id": c.id, "video_id": c.video_id, "error": c.error} for c in failed_clips
            ],
            "failed_stages": [
                {"id": j.id, "stage": j.stage, "error": j.error} for j in failed_jobs
            ],
            "counts": counts,
        }

    logger.info("Failed-job report (24h): {}", counts)
    total = sum(counts.values())
    if total:
        _notify_slack(
            f":bar_chart: Daily failure report: {counts['videos']} videos, "
            f"{counts['clips']} clips, {counts['stages']} stages failed in the last 24h."
        )
    return report
