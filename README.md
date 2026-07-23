# ◈ AI Clip Farm (Local-First)

Turn any long-form video (YouTube URL or local MP4) into **10–15 ready-to-post
vertical clips** — transcribed, intelligently selected, speaker-tracked,
subtitled, and packaged with titles, hooks, descriptions and hashtags.

Everything that *can* run locally does: Whisper transcribes on your machine,
FFmpeg + OpenCV/MediaPipe do all the video work locally. Only the two "brains"
steps — picking the best moments and writing metadata — call the Claude API.

**Production-hardened:** authenticated + rate-limited API, non-root
multi-stage Docker image, structured logging + Prometheus metrics, automatic
retries with exponential backoff, disk-retention cleanup, CI (lint + test +
Docker build), and a deployment guide covering Ubuntu/Windows/DigitalOcean/
Hetzner/AWS. See [Production](#-production) below.

```
YouTube / MP4  ─►  Whisper  ─►  Claude (select)  ─►  FFmpeg cut
                                                        ─►  9:16 speaker track
                                                        ─►  animated subtitles
                                                        ─►  Claude (metadata)
                                                        ─►  organised output folder
```

---

## ✨ What you get per clip

```
data/output/<video-title>/01_<clip-title>/
├── clip.mp4          # 1080×1920, speaker-tracked, burned-in animated captions
├── thumbnail.jpg
└── metadata.json     # title, hook, description, hashtags, score, why-selected
```

---

## 🧱 Architecture

| Service    | Role                                                    | Port |
|------------|---------------------------------------------------------|------|
| **api**    | FastAPI — REST API + modern web dashboard               | 8000 |
| **worker** | Celery — runs the heavy pipeline (Whisper/FFmpeg/Claude)| —    |
| **postgres** | Metadata store (videos, clips, jobs)                  | 5432 |
| **redis**  | Celery broker + result backend                          | 6379 |
| **flower** | Celery queue monitor                                    | 5555 |
| **n8n**    | Optional automation (batch/scheduled ingestion)         | 5678 |

The pipeline is split into independent, swappable stages
(`app/pipeline/*.py`). Each stage does one thing and is called by the
orchestrator. Swap Whisper for a GPU backend, MediaPipe for another tracker, or
Claude Opus for Sonnet — all via `.env`, no code changes.

```
app/
├── main.py                 # FastAPI app: lifespan, CORS, exception handlers,
│                           #   /health + /health/ready + /metrics, request-ID middleware
├── core/
│   ├── config.py           # single source of truth for all settings (.env), fail-fast validation
│   ├── database.py         # SQLAlchemy engine/session + readiness check
│   ├── logging.py          # console (dev) or structured JSON (prod) logs
│   ├── exceptions.py       # typed exception hierarchy (.retryable drives Celery retry policy)
│   ├── validation.py       # SSRF/path-traversal/file-size/corrupted-file guards
│   ├── security.py         # API key auth + Redis-backed rate limiting
│   ├── metrics.py          # Prometheus metrics (stage timing, Claude usage, queue depth)
│   ├── cleanup.py          # work-dir retention (per-clip, per-video, hourly safety net)
│   └── gpu.py              # CUDA/NVENC auto-detection with graceful CPU fallback
├── models/schema.py        # DB schema: Video, Clip, Job (indexed, with timing/retry columns)
├── pipeline/
│   ├── ingest.py           # 1. yt-dlp / local file (validated + size-capped)
│   ├── transcribe.py       # 2. Whisper (word-level timestamps, cached model per process)
│   ├── analyze.py          # 3. Claude selects + ranks clips (structured output)
│   ├── cut.py              # 4. FFmpeg segment cut
│   ├── reframe.py          # 5. 9:16 + speaker tracking (OpenCV/MediaPipe, strided detection)
│   ├── subtitles.py        # 6. animated ASS captions, burned in (escaped, hw-accel-aware)
│   ├── metadata.py         # 7. Claude writes title/hook/description/hashtags
│   ├── claude_client.py    # shared Claude wrapper (retries, structured output, metrics)
│   ├── ffmpeg_utils.py     # typed ffmpeg/ffprobe helpers (timeouts, hw-accel + fallback)
│   └── orchestrator.py     # chains stages, persists state, cleans up, times every stage
├── workers/                # Celery app + tasks (prepare → fan-out render → finalize)
│                           #   + Beat schedule (hourly cleanup, daily failure report)
├── api/                    # routes (auth + rate-limited) + request/response schemas
└── web/                    # vanilla-JS dashboard (index.html, app.js, styles.css)
alembic/                    # DB migrations
tests/                      # unit + integration suite (pytest, mocked externals)
n8n/workflows/              # importable automation workflows
nginx/                      # production TLS reverse-proxy config
.github/workflows/          # CI (lint/test/build) + Docker publish + release
docs/                       # install, architecture, deployment, performance, security, monitoring
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full data flow and the
[database schema](docs/ARCHITECTURE.md#database-schema).

---

## 🚀 Quick start (Docker)

```bash
# 1. Configure
cp .env.example .env
#    → set ANTHROPIC_API_KEY, change the DB/n8n passwords

# 2. Launch the whole stack
docker compose up -d --build

# 3. Open the dashboard
open http://localhost:8000
```

Paste a YouTube URL (or upload an MP4), and watch the stages light up. Rendered
clips appear as thumbnails; click any clip to preview, read its metadata, and
download the MP4.

First run downloads the Whisper model (~1.5 GB for `large-v3`) into a shared
volume — subsequent runs are instant. Use `WHISPER_MODEL=small` for a fast,
low-resource start.

Full setup (GPU, local dev without Docker, troubleshooting) is in
[`docs/INSTALL.md`](docs/INSTALL.md).

---

## 🔌 API

| Method | Path                          | Purpose                              |
|--------|-------------------------------|--------------------------------------|
| POST   | `/api/videos`                 | Submit a YouTube URL / local file    |
| POST   | `/api/videos/upload`          | Upload an MP4 (multipart, size/type validated) |
| GET    | `/api/videos`                 | Paginated video list (`?limit&offset&status`) |
| GET    | `/api/videos/{id}`            | Job detail + clips + stage progress  |
| GET    | `/api/clips/{id}`             | Clip metadata                        |
| GET    | `/api/clips/{id}/download`    | Download the rendered MP4            |
| GET    | `/api/clips/{id}/thumbnail`   | Clip thumbnail                       |
| POST   | `/api/clips/{id}/rerender`    | Re-render a single clip              |
| GET    | `/api/jobs/failed`            | Recent failed videos/clips           |
| POST   | `/api/jobs/failed/report`     | Trigger the failure-report task now  |
| GET    | `/health` / `/health/ready`   | Liveness / readiness (DB check)      |
| GET    | `/metrics`                    | Prometheus metrics                   |

Every `/api/*` route requires `X-API-Key: <key>` once `API_KEY` is set (see
[Security](#-security)) and is rate-limited (`RATE_LIMIT_PER_MINUTE`).

```bash
curl -X POST http://localhost:8000/api/videos \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"source_type":"youtube","source_ref":"https://youtu.be/XXXX"}'
```

---

## ⚙️ Key configuration (`.env`)

| Variable              | Default          | Notes                                   |
|-----------------------|------------------|-----------------------------------------|
| `ENVIRONMENT`         | `development`    | `production` enforces `ANTHROPIC_API_KEY`/`API_KEY`/CORS at startup |
| `API_KEY`             | *(empty)*        | Required in production — protects every `/api/*` route |
| `CLAUDE_MODEL`        | `claude-opus-4-8`| `claude-sonnet-5` = cheaper/faster      |
| `CLAUDE_EFFORT`       | `high`           | `low`→`max` reasoning depth             |
| `WHISPER_MODEL`       | `large-v3`       | `small`/`medium` for speed              |
| `WHISPER_DEVICE`      | `auto`           | Probes for CUDA, falls back to CPU automatically |
| `MIN/MAX_CLIPS_PER_VIDEO` | `10` / `15`  | How many moments to select              |
| `MIN/MAX_CLIP_SECONDS`| `15` / `90`      | Clip duration bounds                    |
| `TRACKING_BACKEND`    | `mediapipe`      | `opencv` / `center`                     |
| `FACE_DETECT_STRIDE`  | `3`              | Detect a face every Nth frame (speed lever) |
| `FFMPEG_HWACCEL`      | `auto`           | Probes for NVENC, falls back to libx264 automatically |
| `SUBTITLE_STYLE`      | `karaoke_bold`   | `clean_white` (see `subtitles.py`)      |
| `RENDER_CONCURRENCY`  | `2`              | Clips rendered in parallel per worker   |
| `CLEANUP_WORK_DIR_ON_SUCCESS` | `true`  | Reclaim disk after each successful render |

Full list (60+ documented settings, including rate limits, retention, and
observability): [`.env.example`](.env.example).

---

## 🤖 Automate with n8n

Import the workflows in `n8n/workflows/` at http://localhost:5678:

- **Submit & Notify** — webhook → submit → poll → return clip list. Wire it to a
  form, Slack, or a Zap.
- **Nightly Batch** — scheduled trigger reads a URL list (swap the Code node for
  Google Sheets / Notion / a DB) and submits them one at a time.

`CLIPFARM_API` is pre-set to `http://api:8000` inside the Docker network.

---

## 📈 Batch processing & scaling

- Clip rendering fans out across the Celery pool (`chord`) — scale by raising
  `RENDER_CONCURRENCY` or running more `worker` replicas
  (`docker compose up -d --scale worker=3`).
- Whisper models are shared via the `whisper-cache` volume, so extra workers
  don't re-download; the model is also cached per-process, not reloaded per video.
  A `beat` service runs hourly cleanup and a daily failure-report task on a
  separate `maintenance` queue so housekeeping never competes with rendering.
- Monitor queues at http://localhost:5555 (Flower, BasicAuth-protected).
- Detailed scaling guidance (GPU workers, connection pool sizing, queue
  separation) for "hundreds of videos/day": [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

---

## 🏭 Production

| Concern | Doc |
|---|---|
| Deploying to Ubuntu/Windows/DigitalOcean/Hetzner/AWS, TLS, systemd, backups | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Security audit findings + fixes, operational checklist | [`docs/SECURITY_CHECKLIST.md`](docs/SECURITY_CHECKLIST.md) |
| What was optimized and why, disk sizing, scaling levers | [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) |
| Health checks, Prometheus metrics, logs, queue/failure monitoring | [`docs/MONITORING.md`](docs/MONITORING.md) |

```bash
# Production stack (TLS via nginx + certbot, tightened resource limits)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Testing & CI

```bash
pip install -r requirements-dev.txt
pytest                 # unit + integration suite (mocked externals; SQLite test DB)
ruff check app tests   # lint
mypy app                # type check (advisory in CI until fully typed)
```

GitHub Actions (`.github/workflows/`) runs lint + tests + a Docker build on
every push/PR, publishes images to `ghcr.io` on `main`/tags, and cuts a
GitHub Release with changelog on version tags. See
[`docs/PERFORMANCE.md`](docs/PERFORMANCE.md#what-was-and-wasnt-verified) for
an important caveat on what has and hasn't actually been run.

---

## 🗺️ Roadmap / future improvements

See [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md#future-improvements).
Highlights: B-roll / zoom auto-editing, per-platform aspect variants, direct
publishing to TikTok/YouTube/Instagram APIs, A/B thumbnail generation, a
"virality predictor" fine-tune, multi-speaker diarization, and a local-LLM
fallback for fully offline metadata.

---

## License

MIT — see `LICENSE`.
