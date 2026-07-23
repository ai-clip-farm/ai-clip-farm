"""Prometheus metrics for both the API process and Celery workers.

Two separate registries in practice:
  - FastAPI process: `prometheus-fastapi-instrumentator` auto-instruments
    HTTP request latency/count/status and exposes GET /metrics.
  - Celery worker process: this module's `PIPELINE_*` metrics, exposed via a
    small dedicated HTTP server started once per worker process (see
    `start_worker_metrics_server`), so Prometheus can scrape workers directly
    even though they have no other HTTP surface.

Both use the default `prometheus_client` global registry — safe here because
API and worker are always separate OS processes (never imported into the same
process), so there's no risk of double-registration.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from app.core.logging import logger

# --- Pipeline stage metrics (used by workers/tasks.py + orchestrator.py) ----

STAGE_DURATION = Histogram(
    "clipfarm_stage_duration_seconds",
    "Wall-clock time spent in each pipeline stage",
    ["stage"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 3600, 7200),
)

STAGE_RESULT = Counter(
    "clipfarm_stage_result_total",
    "Pipeline stage completions by outcome",
    ["stage", "outcome"],  # outcome: success | failure | retry
)

VIDEOS_IN_FLIGHT = Gauge(
    "clipfarm_videos_in_flight",
    "Videos currently being prepared or rendered",
)

CLIPS_RENDERED = Counter(
    "clipfarm_clips_rendered_total",
    "Clips that finished rendering, by outcome",
    ["outcome"],
)

CLAUDE_REQUESTS = Counter(
    "clipfarm_claude_requests_total",
    "Claude API calls made, by purpose and outcome",
    ["purpose", "outcome"],  # purpose: analyze | metadata
)

CLAUDE_TOKENS = Counter(
    "clipfarm_claude_tokens_total",
    "Claude token usage",
    ["purpose", "kind"],  # kind: input | output
)

WORK_DIR_BYTES = Gauge(
    "clipfarm_work_dir_bytes",
    "Total size of the working-directory tree at last cleanup pass",
)

_worker_metrics_server_started = False


def start_worker_metrics_server(port: int) -> None:
    """Start the Prometheus HTTP server exactly once per worker process.

    Celery's prefork pool forks per child; guard with a module-level flag so
    a re-imported module in the same process doesn't try to bind twice.
    """
    global _worker_metrics_server_started
    if _worker_metrics_server_started:
        return
    try:
        start_http_server(port)
        _worker_metrics_server_started = True
        logger.info("Worker metrics server listening on :{}/metrics", port)
    except OSError as e:
        # Common under prefork when multiple children share a metrics port;
        # log and continue rather than crashing the worker.
        logger.warning("Could not start worker metrics server on :{}: {}", port, e)


class StageTimer:
    """Context manager: `with StageTimer("transcribe"): ...` records duration
    and success/failure into the histogram + counter above."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self._start: float | None = None

    def __enter__(self) -> StageTimer:
        import time

        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import time

        elapsed = time.monotonic() - (self._start or 0)
        STAGE_DURATION.labels(stage=self.stage).observe(elapsed)
        STAGE_RESULT.labels(stage=self.stage, outcome="failure" if exc_type else "success").inc()
