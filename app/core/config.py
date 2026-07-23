"""Centralised configuration, loaded once from the environment.

Every module imports the singleton `settings` — there is no other source of
truth for tunables. This keeps the pipeline modular: swap a model, a path, or a
tracking backend by changing one env var, never the code.

All enum-like fields use `Literal` so a typo in `.env` (e.g. `WHISPER_DEVICE=gpu`
instead of `cuda`) fails fast at process startup instead of surfacing as an
obscure runtime error hours into a batch run.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Runtime environment ---
    environment: Environment = "development"

    # --- Claude ---
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-8"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    claude_timeout_seconds: float = 120.0
    claude_max_retries: int = 5

    # --- Whisper ---
    whisper_backend: Literal["faster_whisper"] = "faster_whisper"
    whisper_model: str = "large-v3"
    whisper_device: Literal["cpu", "cuda", "auto"] = "auto"
    whisper_compute_type: str = "int8"
    # Cache one loaded model per worker process instead of reloading per task.
    whisper_cache_models: bool = True
    # "translate" always produces English text regardless of the spoken
    # language (a no-op for already-English audio) — the default audience for
    # generated clips is English-speaking, so subtitles/transcript/downstream
    # Claude prompts are English by default even when the source video isn't.
    # Set to "transcribe" to keep the original spoken language instead.
    whisper_task: Literal["transcribe", "translate"] = "translate"

    # --- Clip selection ---
    min_clips_per_video: int = 10
    max_clips_per_video: int = 15
    min_clip_seconds: int = 15
    max_clip_seconds: int = 90

    # --- Rendering ---
    target_width: int = 1080
    target_height: int = 1920
    target_fps: int = 30
    tracking_backend: Literal["mediapipe", "opencv", "center"] = "mediapipe"
    subtitle_style: Literal["karaoke_bold", "clean_white"] = "karaoke_bold"
    # Run face detection every Nth frame and interpolate between hits — the
    # single biggest reframe.py speedup (5-10x fewer MediaPipe calls) with no
    # visible smoothness loss because the trajectory is already EMA-smoothed.
    face_detect_stride: int = 3
    # FFmpeg hardware encoder: "auto" probes for NVENC/VAAPI/QSV and falls
    # back to libx264 if none is available.
    ffmpeg_hwaccel: Literal["auto", "none", "nvenc", "vaapi", "qsv"] = "auto"
    ffmpeg_preset: str = "veryfast"
    ffmpeg_crf: int = 20
    # Hard wall-clock cap per ffmpeg invocation so a stuck process can't hold
    # a worker slot forever.
    ffmpeg_timeout_seconds: int = 1800

    # --- Storage / retention ---
    data_dir: Path = Path("/data")
    input_dir: Path = Path("/data/input")
    work_dir: Path = Path("/data/work")
    output_dir: Path = Path("/data/output")
    # Delete the per-video working directory after a clip finishes rendering
    # successfully. Without this, `data/work` grows without bound.
    cleanup_work_dir_on_success: bool = True
    keep_work_dir_on_failure: bool = True
    # Safety-net Celery Beat task purges anything older than this, regardless
    # of the flag above (covers crashes that skip the normal cleanup path).
    work_dir_retention_hours: int = 48
    max_upload_size_mb: int = 2048
    max_download_size_mb: int = 4096

    # --- Database / queue ---
    database_url: str = "postgresql+psycopg://clipfarm:clipfarm@postgres:5432/clipfarm"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    render_concurrency: int = 2
    celery_task_soft_time_limit: int = 3600 * 2  # allow cleanup before kill
    celery_task_time_limit: int = 3600 * 3  # hard SIGKILL ceiling

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    log_json: bool = False
    cors_origins: str = "http://localhost:8000"  # comma-separated
    docs_enabled: bool = True

    # --- Security ---
    # When set, every /api/* request must send `X-API-Key: <value>` (or
    # Authorization: Bearer <value>). Empty in dev = auth disabled — the API
    # refuses to boot with an empty key in production (see validator below).
    api_key: str = ""
    rate_limit_per_minute: int = 30
    rate_limit_upload_per_hour: int = 20
    allowed_video_extensions: str = ".mp4,.mov,.mkv,.webm"
    # Only these URL hosts/suffixes may be submitted as YouTube sources.
    # Blocks SSRF via yt-dlp being pointed at internal/private endpoints.
    allowed_source_hosts: str = (
        "youtube.com,www.youtube.com,youtu.be,m.youtube.com,music.youtube.com"
    )

    # --- Observability ---
    metrics_enabled: bool = True
    sentry_dsn: str = ""
    slack_webhook_url: str = ""  # optional failed-job alerting

    def ensure_dirs(self) -> None:
        for d in (self.input_dir, self.work_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_video_extension_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_video_extensions.split(",") if e.strip()}

    @property
    def allowed_source_host_set(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_source_hosts.split(",") if h.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @field_validator("claude_max_retries", "db_pool_size", "render_concurrency")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    @model_validator(mode="after")
    def _validate_production_invariants(self) -> Settings:
        if self.environment == "production":
            problems = []
            if not self.anthropic_api_key:
                problems.append("ANTHROPIC_API_KEY is required in production")
            if not self.api_key:
                problems.append(
                    "API_KEY must be set in production (protects /api/* from anyone on the network)"
                )
            if "*" in self.cors_origin_list:
                problems.append("CORS_ORIGINS must not be '*' in production")
            if problems:
                raise ValueError(
                    "Invalid production configuration:\n  - " + "\n  - ".join(problems)
                )
        return self

    def ensure_runtime_ready(self) -> list[str]:
        """Non-fatal warnings surfaced at startup (logged, not raised) so
        development stays frictionless while production gets loud signals."""
        warnings: list[str] = []
        if not self.anthropic_api_key:
            warnings.append(
                "ANTHROPIC_API_KEY is empty — analysis/metadata stages will fail at runtime"
            )
        if not self.auth_enabled:
            warnings.append(
                "API_KEY is empty — the API is UNAUTHENTICATED. Set API_KEY before exposing this "
                "beyond localhost."
            )
        return warnings


def generate_api_key() -> str:
    """Helper for operators: `python -c 'from app.core.config import generate_api_key; print(generate_api_key())'`"""
    return secrets.token_urlsafe(32)


@lru_cache
def get_settings() -> Settings:
    # Deliberately does NOT call ensure_dirs() here: this factory runs on
    # every import of this module, including by tooling that never touches
    # the filesystem (alembic/env.py, one-off scripts, CI's migration-only
    # job on a bare runner with no /data mount). Forcing directory creation
    # as an import side effect meant `alembic upgrade head` crashed with
    # PermissionError on a host that has no reason to need /data at all.
    # The actual entry points that read/write those directories (the API's
    # lifespan startup, the Celery worker's process-init hook) call
    # ensure_dirs() explicitly instead.
    return Settings()


settings = get_settings()
