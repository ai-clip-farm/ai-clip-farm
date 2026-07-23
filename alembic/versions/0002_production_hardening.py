"""production hardening: indexes, timing + retry columns

Revision ID: 0002_production_hardening
Revises: 0001_initial
Create Date: 2026-02-01

Adds:
  - status/lookup indexes on videos, clips, jobs (queried heavily by the
    dashboard and the failed-jobs report once the DB holds thousands of rows)
  - Clip.retry_count, Clip.render_started_at, Clip.render_finished_at
  - Job.retry_count
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_production_hardening"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("retry_count", sa.Integer, server_default="0", nullable=False))
    op.add_column("clips", sa.Column("render_started_at", sa.DateTime, nullable=True))
    op.add_column("clips", sa.Column("render_finished_at", sa.DateTime, nullable=True))
    op.add_column("jobs", sa.Column("retry_count", sa.Integer, server_default="0", nullable=False))

    op.create_index("ix_videos_status", "videos", ["status"])
    op.create_index("ix_videos_created_at", "videos", ["created_at"])
    op.create_index("ix_clips_status", "clips", ["status"])
    op.create_index("ix_clips_video_id_rank", "clips", ["video_id", "rank"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_video_id_stage", "jobs", ["video_id", "stage"])


def downgrade() -> None:
    op.drop_index("ix_jobs_video_id_stage", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_clips_video_id_rank", table_name="clips")
    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_index("ix_videos_created_at", table_name="videos")
    op.drop_index("ix_videos_status", table_name="videos")

    op.drop_column("jobs", "retry_count")
    op.drop_column("clips", "render_finished_at")
    op.drop_column("clips", "render_started_at")
    op.drop_column("clips", "retry_count")
