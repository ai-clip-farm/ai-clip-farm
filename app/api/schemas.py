"""Pydantic request/response models for the HTTP API (distinct from DB models)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ClipStatus, JobStatus, SourceType


class CreateVideoRequest(BaseModel):
    source_type: SourceType
    source_ref: str          # YouTube URL or filename in INPUT_DIR
    title: str | None = None


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    rank: int
    start_seconds: float
    end_seconds: float
    score: float
    reason: str
    categories: list[str] | None
    gen_title: str
    gen_hook: str
    gen_description: str
    gen_hashtags: list[str] | None
    status: ClipStatus
    output_path: str | None
    thumbnail_path: str | None
    error: str | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    stage: str
    status: JobStatus
    progress: float
    message: str
    error: str | None


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    source_type: SourceType
    source_ref: str
    duration_seconds: float | None
    status: JobStatus
    error: str | None
    created_at: datetime


class VideoDetailOut(VideoOut):
    clips: list[ClipOut]
    jobs: list[JobOut]


class Page(BaseModel):
    """Generic pagination envelope — required once the videos table holds
    more than a page or two of rows (expected at "hundreds/day" volume)."""

    items: list[VideoOut]
    total: int
    limit: int
    offset: int
