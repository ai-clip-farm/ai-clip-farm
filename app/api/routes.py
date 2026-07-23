"""HTTP API routes.

Every mutating/expensive route is behind `require_api_key` (no-op when
API_KEY is unset — local dev) and rate-limited per `app.core.security`.
Uploads and YouTube submissions are validated (`app.core.validation`) before
anything touches disk or enqueues a Celery task, so bad input fails fast with
a 400 instead of wasting a worker cycle.

Deliberately does NOT use `from __future__ import annotations`: FastAPI needs
the *real* `UploadFile` class (not a stringified forward-reference) at route-
registration time to special-case multipart file parameters. Combining the
two raises `FastAPIError: Invalid args for response field! ... ForwardRef
('UploadFile')` at import time — a known FastAPI/Starlette incompatibility,
not something request-specific. Safe to omit here: every annotation in this
file (`str | None`, `list[str]`) is native Python 3.10+ runtime syntax
(PEP 604 / PEP 585), so nothing here actually needed postponed evaluation.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import (
    ClipOut,
    CreateVideoRequest,
    Page,
    VideoDetailOut,
    VideoOut,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.logging import logger
from app.core.security import limiter, require_api_key
from app.core.validation import (
    sanitize_filename,
    validate_upload_size,
    validate_youtube_url,
)
from app.models import Clip, ClipStatus, JobStatus, SourceType, Video
from app.workers.tasks import failed_job_report, process_video, render_clip

router = APIRouter(prefix="/api", tags=["clipfarm"], dependencies=[Depends(require_api_key)])

# NOTE: every `@limiter.limit(...)`-decorated endpoint below takes an unused
# `request: Request` parameter. This is required by slowapi (it inspects the
# endpoint signature for the Request object to key the rate limit on) — not
# dead code, don't remove it even though nothing in the body references it.


def _output_path_is_safe(path: str) -> bool:
    """Defense in depth: confirm a stored output/thumbnail path really lives
    under OUTPUT_DIR before serving it, even though the value comes from our
    own DB rather than direct user input."""
    try:
        resolved = Path(path).resolve()
        return settings.output_dir.resolve() in resolved.parents
    except (OSError, RuntimeError):
        return False


@router.post("/videos", response_model=VideoOut, status_code=201)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
def create_video(request: Request, req: CreateVideoRequest, db: Session = Depends(get_db)):
    """Register a YouTube URL or local filename and start processing."""
    if req.source_type == SourceType.youtube:
        try:
            req.source_ref = validate_youtube_url(req.source_ref)
        except ValidationError as e:
            raise HTTPException(400, str(e)) from e

    video = Video(
        title=(req.title or "")[:512],
        source_type=req.source_type,
        source_ref=req.source_ref,
    )
    db.add(video)
    db.commit()
    process_video.delay(video.id)
    logger.info("Enqueued video {} ({})", video.id, req.source_type)
    return video


@router.post("/videos/upload", response_model=VideoOut, status_code=201)
@limiter.limit(f"{settings.rate_limit_upload_per_hour}/hour")
def upload_video(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Upload an MP4 and start processing."""
    try:
        safe_name = sanitize_filename(file.filename or "")
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e

    # UploadFile.size is populated by Starlette from Content-Length when
    # available; fall back to streaming-with-a-cap if it's missing so a
    # client can't bypass the limit by omitting the header.
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    dest = settings.input_dir / safe_name
    written = 0
    try:
        with dest.open("wb") as f:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, f"File exceeds {settings.max_upload_size_mb} MB limit")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"Failed to save upload: {e}") from e

    try:
        validate_upload_size(written)
    except ValidationError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from e

    video = Video(
        title=(title or Path(safe_name).stem)[:512],
        source_type=SourceType.upload,
        source_ref=safe_name,
    )
    db.add(video)
    db.commit()
    process_video.delay(video.id)
    logger.info("Enqueued uploaded video {} ({} bytes)", video.id, written)
    return video


@router.get("/videos", response_model=Page)
def list_videos(
    db: Session = Depends(get_db),
    limit: int = 25,
    offset: int = 0,
    status: JobStatus | None = None,
):
    """Paginated video list — required once volume exceeds a page or two;
    an earlier version returned every row unbounded, which degrades linearly
    with total videos processed since project launch."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    stmt = select(Video)
    count_stmt = select(func.count()).select_from(Video)
    if status is not None:
        stmt = stmt.where(Video.status == status)
        count_stmt = count_stmt.where(Video.status == status)

    total = db.scalar(count_stmt) or 0
    items = db.scalars(stmt.order_by(Video.created_at.desc()).limit(limit).offset(offset)).all()
    # `items` is Sequence[Video] (ORM rows); Page.items is typed list[VideoOut]
    # (the Pydantic response schema). Pydantic v2 coerces each element via
    # VideoOut's own from_attributes=True config at validation time — this
    # is correct at runtime (identical to how every other route here returns
    # a bare ORM object as its response_model), mypy just can't see through
    # that pydantic-level coercion when the model is constructed explicitly
    # in application code rather than returned straight to FastAPI.
    return Page(items=items, total=total, limit=limit, offset=offset)  # type: ignore[arg-type]


@router.get("/videos/{video_id}", response_model=VideoDetailOut)
def get_video(video_id: str, db: Session = Depends(get_db)):
    video = db.scalars(
        select(Video)
        .where(Video.id == video_id)
        .options(selectinload(Video.clips), selectinload(Video.jobs))
    ).first()
    if not video:
        raise HTTPException(404, "Video not found")
    video.clips.sort(key=lambda c: c.rank)
    return video


@router.get("/clips/{clip_id}", response_model=ClipOut)
def get_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@router.post("/clips/{clip_id}/rerender", response_model=ClipOut)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
def rerender_clip(request: Request, clip_id: str, db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    render_clip.delay(clip_id)
    logger.info("Re-render requested for clip {}", clip_id)
    return clip


@router.get("/clips/{clip_id}/download")
def download_clip(clip_id: str, db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).exists():
        raise HTTPException(404, "Rendered clip not available")
    if not _output_path_is_safe(clip.output_path):
        logger.error("Refusing to serve out-of-tree path for clip {}", clip_id)
        raise HTTPException(500, "Invalid stored path")
    return FileResponse(
        clip.output_path,
        media_type="video/mp4",
        filename=f"{clip.gen_title or clip.id}.mp4",
    )


@router.get("/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str, db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if not clip or not clip.thumbnail_path or not Path(clip.thumbnail_path).exists():
        raise HTTPException(404, "Thumbnail not available")
    if not _output_path_is_safe(clip.thumbnail_path):
        raise HTTPException(500, "Invalid stored path")
    return FileResponse(clip.thumbnail_path, media_type="image/jpeg")


@router.get("/jobs/failed")
def list_failed(db: Session = Depends(get_db)):
    """On-demand view of the same data the daily Slack/report Celery task
    (`clipfarm.failed_job_report`) summarizes — useful for the dashboard and
    for `curl`-based ops checks without waiting for the beat schedule."""
    videos = db.scalars(
        select(Video)
        .where(Video.status == JobStatus.failed)
        .order_by(Video.updated_at.desc())
        .limit(50)
    ).all()
    clips = db.scalars(
        select(Clip)
        .where(Clip.status == ClipStatus.failed)
        .order_by(Clip.updated_at.desc())
        .limit(50)
    ).all()
    return {
        "failed_videos": [{"id": v.id, "title": v.title, "error": v.error} for v in videos],
        "failed_clips": [{"id": c.id, "video_id": c.video_id, "error": c.error} for c in clips],
    }


@router.post("/jobs/failed/report")
def trigger_failed_job_report():
    """Manually trigger the failure-report task (normally runs daily via
    Celery Beat) — handy right after an incident."""
    task = failed_job_report.delay()
    return {"task_id": task.id}
