# Monitoring Guide

## Health checks

| Endpoint | Purpose | Checks |
|---|---|---|
| `GET /health` | Liveness — "is the process up?" | Nothing external — always 200 if the process is running. Used by Docker's `HEALTHCHECK` and any load balancer's basic liveness probe. |
| `GET /health/ready` | Readiness — "can it actually serve traffic?" | Database connectivity (`check_db_connection()`). Returns 503 if not ready. Use this for load-balancer routing decisions, not `/health`. |
| Worker: `celery inspect ping` | Worker liveness | Used by the `worker` service's Docker healthcheck (see `docker-compose.yml`). |

Why two separate liveness/readiness endpoints: a load balancer that restarts
a container on any dependency blip (using liveness semantics for a
readiness question) causes unnecessary restarts and can create a restart
storm under a brief Postgres failover. Route traffic based on `/health/ready`;
restart policy based on `/health`.

## Metrics (Prometheus)

Two scrape targets:

- **API**: `GET /metrics` — HTTP request count/latency/status, auto-instrumented
  by `prometheus-fastapi-instrumentator`.
- **Worker**: `GET :9100/metrics` (port configurable via `WORKER_METRICS_PORT`)
  — pipeline-specific metrics defined in `app/core/metrics.py`:

| Metric | Type | Labels | What it tells you |
|---|---|---|---|
| `clipfarm_stage_duration_seconds` | Histogram | `stage` | Execution time per pipeline stage (ingest/transcribe/analyze/cut/reframe/subtitles/burn_subtitles/metadata) — the "execution times" requirement. |
| `clipfarm_stage_result_total` | Counter | `stage`, `outcome` | Success/failure count per stage. |
| `clipfarm_videos_in_flight` | Gauge | — | Videos currently in `prepare_video`. Sustained saturation at your worker concurrency = queue-bound, add workers. |
| `clipfarm_clips_rendered_total` | Counter | `outcome` | Overall clip render success/failure count. |
| `clipfarm_claude_requests_total` | Counter | `purpose`, `outcome` | Claude API call outcomes (`success`/`refusal`/`error`/`empty`/`invalid_schema`) split by `analyze`/`metadata`. |
| `clipfarm_claude_tokens_total` | Counter | `purpose`, `kind` | Token usage (`input`/`output`) — feed into your own cost dashboard. |
| `clipfarm_work_dir_bytes` | Gauge | — | Working-directory size at last cleanup pass — an early-warning signal before a full disk. |

### Example Prometheus scrape config

```yaml
scrape_configs:
  - job_name: clipfarm-api
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics

  - job_name: clipfarm-worker
    static_configs:
      - targets: ["worker:9100"]
```

> **Multiple worker replicas:** Docker's embedded DNS round-robins the
> `worker` hostname across replicas, so a single static target scrapes a
> random replica each interval rather than all of them individually. For
> per-replica breakdown at scale, either run Prometheus with Docker Swarm/
> Consul service discovery, or (simpler) accept aggregate-only visibility
> from this scrape and rely on `celery inspect stats` for per-replica
> debugging when needed. This is a documented tradeoff, not an oversight —
> see PERFORMANCE.md § Scaling.

### Suggested Grafana panels

- `histogram_quantile(0.95, rate(clipfarm_stage_duration_seconds_bucket[5m]))` by `stage`
  — p95 latency per stage, the fastest way to see which stage regressed after a deploy.
- `sum(rate(clipfarm_stage_result_total{outcome="failure"}[1h])) by (stage)` — failure
  rate per stage.
- `clipfarm_videos_in_flight` — a flat line pinned at your worker concurrency for
  extended periods means you're under-provisioned.
- `sum(rate(clipfarm_claude_tokens_total[1d])) by (purpose)` — daily Claude spend proxy.

## Logs

`LOG_JSON=true` in production emits one JSON object per line (`app/core/logging.py`),
ready for Loki/ELK/CloudWatch. Every API request is tagged with a `request_id`
(via the `X-Request-ID` response header and `logger.contextualize`), so a single
request's full log trail — including into any synchronous Claude/DB calls it
triggered — can be found with one query:

```
{app="clipfarm-api"} | json | request_id="a1b2c3d4-..."
```

Celery tasks don't share that request ID (they're dispatched asynchronously),
but each task's own ID is logged at start/end via Celery's `task_track_started`
and can be correlated by `video_id`/`clip_id`, which every pipeline log line
includes.

## Queue monitoring (Flower)

`https://<domain>/flower/` (BasicAuth-protected — see SECURITY_CHECKLIST.md).
Shows real-time task counts, active/reserved/scheduled tasks per worker,
and lets you inspect or revoke a specific stuck task. This is your first
stop when "videos aren't processing" — check whether tasks are queued but
not being picked up (worker down or wrong queue) versus actively running
but slow (check `clipfarm_stage_duration_seconds` for which stage).

## Failed-job reporting

Two ways to see the same underlying data:

1. **On demand**: `GET /api/jobs/failed` (recent failed videos/clips) or
   `POST /api/jobs/failed/report` (triggers the full 24h report task
   immediately instead of waiting for its daily schedule).
2. **Scheduled**: `clipfarm.failed_job_report` runs daily via Celery Beat
   (see `beat_schedule` in `app/workers/celery_app.py`), and posts a summary
   to `SLACK_WEBHOOK_URL` if configured — set that up so failures surface
   without anyone needing to remember to check a dashboard.

Per-stage failure detail (which stage failed, with what error) lives in the
`jobs` table, queryable directly if you need more than the report task's
summary:

```sql
SELECT video_id, stage, error, updated_at
FROM jobs
WHERE status = 'failed' AND updated_at > now() - interval '24 hours'
ORDER BY updated_at DESC;
```

## Execution-time tracking

Every `Job` row has a computed `duration_seconds` property
(`updated_at - created_at`, once terminal); every `Clip` row tracks
`render_started_at`/`render_finished_at` explicitly (`render_duration_seconds`
property). Combined with the `clipfarm_stage_duration_seconds` Prometheus
histogram, you have both per-run (DB) and aggregate-trend (Prometheus) views
of the same underlying timing data — use the DB for "why was *this* video
slow," Prometheus for "has *stage X* been getting slower over the last week."
