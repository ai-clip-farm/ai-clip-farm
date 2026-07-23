"""Database schema.

Three tables model the whole pipeline:

    Video  1───*  Clip
      │
      └──1───*  Job     (one row per pipeline stage run, for observability)

A `Video` is an ingested long-form source. Each `Clip` is one selected moment
that gets cut, reframed, subtitled and packaged with generated metadata. `Job`
rows record every async stage so the UI can show live progress and failures.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class SourceType(str, enum.Enum):
    youtube = "youtube"
    upload = "upload"
    local = "local"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class ClipStatus(str, enum.Enum):
    selected = "selected"  # chosen by Claude, not yet rendered
    rendering = "rendering"
    completed = "completed"
    failed = "failed"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512), default="")
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType))
    source_ref: Mapped[str] = mapped_column(Text)  # URL or original filename
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # local mp4
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending)
    # Full Whisper transcript with word-level timestamps (segments + words).
    transcript: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    clips: Mapped[list[Clip]] = relationship(back_populates="video", cascade="all, delete-orphan")
    jobs: Mapped[list[Job]] = relationship(back_populates="video", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_videos_status", "status"),
        Index("ix_videos_created_at", "created_at"),
    )


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)  # 1 = strongest moment

    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)

    # --- Claude scoring (why this moment was chosen) ---
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100 viral potential
    reason: Mapped[str] = mapped_column(Text, default="")
    categories: Mapped[list | None] = mapped_column(JSON, nullable=True)  # hook/funny/…
    transcript_text: Mapped[str] = mapped_column(Text, default="")

    # --- Generated metadata ---
    gen_title: Mapped[str] = mapped_column(String(512), default="")
    gen_hook: Mapped[str] = mapped_column(Text, default="")
    gen_description: Mapped[str] = mapped_column(Text, default="")
    gen_hashtags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # --- Output artefacts ---
    status: Mapped[ClipStatus] = mapped_column(Enum(ClipStatus), default=ClipStatus.selected)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Observability ---
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    render_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    render_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    video: Mapped[Video] = relationship(back_populates="clips")

    __table_args__ = (
        Index("ix_clips_status", "status"),
        Index("ix_clips_video_id_rank", "video_id", "rank"),
    )

    @property
    def duration(self) -> float:
        return round(self.end_seconds - self.start_seconds, 2)

    @property
    def render_duration_seconds(self) -> float | None:
        if self.render_started_at and self.render_finished_at:
            return round((self.render_finished_at - self.render_started_at).total_seconds(), 2)
        return None


class Job(Base):
    """One row per pipeline stage execution, for progress + debugging."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))  # ingest/transcribe/analyze/render
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0 - 1.0
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    video: Mapped[Video] = relationship(back_populates="jobs")

    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_video_id_stage", "video_id", "stage"),
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.status in (JobStatus.completed, JobStatus.failed):
            return round((self.updated_at - self.created_at).total_seconds(), 2)
        return None
