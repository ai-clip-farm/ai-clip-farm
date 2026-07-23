"""Celery application instance.

Two queues (`pipeline` for prepare/render, `maintenance` for periodic
housekeeping) so a burst of clip rendering can never starve out cleanup or
report generation — run maintenance workers with
`-Q maintenance --concurrency=1` if you want strict isolation, or leave both
queues on the default worker for smaller deployments.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings

celery_app = Celery(
    "clipfarm",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,  # re-queue on worker crash
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # heavy tasks: one at a time per slot
    worker_max_tasks_per_child=20,  # recycle to release ffmpeg/whisper mem
    worker_send_task_events=True,  # required for Flower + task-level metrics
    task_send_sent_event=True,
    result_expires=60 * 60 * 24,
    task_soft_time_limit=settings.celery_task_soft_time_limit,  # SIGTERM: allow cleanup
    task_time_limit=settings.celery_task_time_limit,  # SIGKILL: hard ceiling
    task_default_queue="pipeline",
    task_routes={
        "clipfarm.process_video": {"queue": "pipeline"},
        "clipfarm.render_clip": {"queue": "pipeline"},
        "clipfarm.finalize_video": {"queue": "pipeline"},
        "clipfarm.purge_stale_work_dirs": {"queue": "maintenance"},
        "clipfarm.failed_job_report": {"queue": "maintenance"},
    },
    beat_schedule={
        "purge-stale-work-dirs-hourly": {
            "task": "clipfarm.purge_stale_work_dirs",
            "schedule": 3600.0,
        },
        "failed-job-report-daily": {
            "task": "clipfarm.failed_job_report",
            "schedule": 24 * 3600.0,
        },
    },
)


@worker_process_init.connect
def _init_worker_metrics(**_kwargs) -> None:
    """Start the per-process Prometheus metrics HTTP server once each worker
    (or prefork child) boots. Port is derived from CELERY_WORKER_METRICS_PORT
    with a safe default; multiple children on the same host should each get a
    distinct port via `--prefetch-multiplier`/process-specific env in compose."""
    import os

    from app.core.metrics import start_worker_metrics_server

    settings.ensure_dirs()

    if settings.metrics_enabled:
        port = int(os.environ.get("WORKER_METRICS_PORT", "9100"))
        start_worker_metrics_server(port)
