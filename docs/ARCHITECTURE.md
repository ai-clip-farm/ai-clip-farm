# Architecture

## Design principles

1. **Local-first.** Transcription (Whisper), cutting/reframing/subtitling
   (FFmpeg + OpenCV/MediaPipe) all run on your hardware. Claude is used only
   where language reasoning is genuinely needed: *which* moments to clip, and
   the copy to publish them with.
2. **Modular stages.** Every step in `app/pipeline/` is a single function with a
   narrow contract. The orchestrator chains them. Any stage can be swapped
   (backend/model/tracker) via `.env` without touching callers.
3. **Async & horizontally scalable.** The API stays responsive; Celery does the
   heavy lifting. Preparation (ingest/transcribe/analyze) runs once per video;
   clip rendering fans out across the worker pool.
4. **Observable.** Every stage writes a `Job` row (status + progress + error),
   so the UI shows live progress and failures are debuggable. Prometheus
   metrics and structured logging extend this from "debuggable after the
   fact" to "monitored in real time" — see `docs/MONITORING.md`.
5. **Fail fast, fail typed.** Every deliberately-raised error is a subclass
   of `ClipFarmError` (`app/core/exceptions.py`) with a `.retryable` flag —
   Celery uses it to distinguish "transient, retry with backoff" (network
   blip, rate limit) from "this input is bad, retrying won't help" (a bad
   URL, a Claude schema mismatch), so a doomed task doesn't burn 2-3 retry
   cycles — each potentially re-running a 20-minute transcription — before
   failing anyway.

## Cross-cutting infrastructure (`app/core/`)

Beyond config/database/logging (Phase 1), the production hardening pass
added:

| Module | Responsibility |
|---|---|
| `exceptions.py` | Typed error hierarchy driving Celery's retry-vs-fail-fast decision. |
| `validation.py` | Every external input (YouTube URL, uploaded filename, local source path, downloaded/uploaded file) is validated *before* it reaches ffmpeg/yt-dlp/Whisper — SSRF, path-traversal, and corrupted-file guards live here, not scattered across call sites. |
| `security.py` | API key auth dependency + Redis-backed rate limiting. |
| `gpu.py` | CUDA/NVENC detection, shared by both the Whisper device resolution and the ffmpeg hardware-encoder selection, so both agree on what hardware is actually usable rather than trusting a possibly-stale env var independently. |
| `cleanup.py` | Work-directory retention (per-clip on success, per-video on finalize, hourly safety-net sweep). |
| `metrics.py` | Prometheus definitions + the `StageTimer` context manager the orchestrator wraps every stage in. |

## Data flow

```
                 ┌─────────────┐
  HTTP / n8n ───►│   FastAPI   │  POST /api/videos  ──► enqueue Celery task
                 └──────┬──────┘
                        │ process_video
                        ▼
        ┌───────────────────────────────────┐
        │  PREPARE (one Celery task)         │
        │  1. ingest      yt-dlp / copy      │
        │  2. transcribe  Whisper (words)    │
        │  3. analyze     Claude → N clips   │  ── creates Clip rows
        └──────────────┬────────────────────┘
                       │ chord: fan out one task per clip
        ┌──────────────▼────────────────────┐
        │  RENDER  (Celery task × N, parallel)│
        │  4. cut         FFmpeg segment     │
        │  5. reframe     9:16 + tracking    │
        │  6. subtitles   animated ASS burn  │
        │  7. metadata    Claude → title/…   │
        │     package →  output/<video>/<clip>│
        └──────────────┬────────────────────┘
                       │ chord callback
                       ▼
                 finalize_video  (status = completed)
```

Splitting **prepare** from **render** means a single failed clip retries in
isolation without re-running Whisper or the selection call, and clips render
concurrently.

## Why word-level timestamps matter

Whisper returns per-word timing. Two stages depend on it:

- **`analyze`** snaps Claude's proposed start/end to the nearest word boundary,
  so cuts never slice through a word and land on natural pauses.
- **`subtitles`** uses per-word timing to drive the karaoke word-by-word
  highlight animation.

## Speaker tracking (`reframe.py`)

1. Detect the largest face per frame (MediaPipe or Haar cascade).
2. Fill frames with no detection using the last known position.
3. Apply an exponential moving average to the horizontal centre → smooth,
   cinematic motion instead of jitter.
4. Render a 9:16 crop window that follows the smoothed centre, then mux the
   original audio back with FFmpeg.

`TRACKING_BACKEND=center` skips detection for a fast static crop.

## Database schema

```
┌──────────────────────── videos ────────────────────────┐
│ id (uuid, pk)                                           │
│ title, source_type(youtube|upload|local), source_ref    │
│ source_path, duration_seconds                           │
│ status(pending|running|completed|failed)                │
│ transcript (JSON: segments[] with word timestamps)      │
│ error, created_at, updated_at                           │
└─────────────┬───────────────────────────┬───────────────┘
              │ 1                          │ 1
              │ *                          │ *
       ┌──────▼──────── clips ─────┐  ┌────▼──────── jobs ──────┐
       │ id (uuid, pk)             │  │ id (uuid, pk)           │
       │ video_id (fk)             │  │ video_id (fk)           │
       │ rank                      │  │ stage (ingest/…/render) │
       │ start_seconds, end_seconds│  │ status                  │
       │ score, reason, categories │  │ progress (0..1)         │
       │ transcript_text           │  │ message, error          │
       │ gen_title, gen_hook,      │  │ created_at, updated_at  │
       │ gen_description,          │  └─────────────────────────┘
       │ gen_hashtags              │
       │ status(selected|rendering │
       │   |completed|failed)      │
       │ output_path, thumbnail_path│
       │ error, created_at, ...    │
       └───────────────────────────┘
```

- **videos** — one ingested long-form source + its full transcript.
- **clips** — one selected moment. Holds Claude's score/reason, generated
  metadata, and the paths of the rendered artefacts.
- **jobs** — one row per pipeline stage run, purely for progress/observability.

Definitions live in [`app/models/schema.py`](../app/models/schema.py); the
matching migration is [`alembic/versions/0001_initial.py`](../alembic/versions/0001_initial.py).

## Claude usage

Both Claude calls use the Anthropic SDK's `messages.parse()` with a Pydantic
schema (structured outputs) — no brittle string parsing — plus adaptive
thinking and the configurable `effort` level. Requests retry with exponential
backoff on transient errors (`claude_client.py`).

- **`analyze`** — one call per video. Sends the compact timestamped transcript,
  gets back a ranked `clips[]` array scored on hook / emotion / value / humor /
  virality.
- **`metadata`** — one focused call per clip for title/hook/description/hashtags,
  fanned out across the worker pool.
