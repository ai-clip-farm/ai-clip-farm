"""initial schema: videos, clips, jobs

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01

This mirrors app/models/schema.py. In development `Base.metadata.create_all`
(called on API startup) is enough; run this migration in production instead:

    alembic upgrade head
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

source_type = sa.Enum("youtube", "upload", "local", name="sourcetype")
job_status = sa.Enum("pending", "running", "completed", "failed", name="jobstatus")
clip_status = sa.Enum("selected", "rendering", "completed", "failed", name="clipstatus")


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("source_type", source_type, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column("source_path", sa.Text),
        sa.Column("duration_seconds", sa.Float),
        sa.Column("status", job_status, nullable=False, server_default="pending"),
        sa.Column("transcript", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_table(
        "clips",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), index=True),
        sa.Column("rank", sa.Integer, server_default="0"),
        sa.Column("start_seconds", sa.Float, nullable=False),
        sa.Column("end_seconds", sa.Float, nullable=False),
        sa.Column("score", sa.Float, server_default="0"),
        sa.Column("reason", sa.Text, server_default=""),
        sa.Column("categories", sa.JSON),
        sa.Column("transcript_text", sa.Text, server_default=""),
        sa.Column("gen_title", sa.String(512), server_default=""),
        sa.Column("gen_hook", sa.Text, server_default=""),
        sa.Column("gen_description", sa.Text, server_default=""),
        sa.Column("gen_hashtags", sa.JSON),
        sa.Column("status", clip_status, nullable=False, server_default="selected"),
        sa.Column("output_path", sa.Text),
        sa.Column("thumbnail_path", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), index=True),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="pending"),
        sa.Column("progress", sa.Float, server_default="0"),
        sa.Column("message", sa.Text, server_default=""),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("clips")
    op.drop_table("videos")
    clip_status.drop(op.get_bind(), checkfirst=True)
    job_status.drop(op.get_bind(), checkfirst=True)
    source_type.drop(op.get_bind(), checkfirst=True)
