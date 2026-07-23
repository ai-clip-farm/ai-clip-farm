# Installation Guide

Two supported paths: **Docker (recommended)** and **local development**.

This guide covers getting the stack running locally. For a hardened,
always-on deployment (TLS, auth, monitoring, backups, cloud-provider-specific
steps), see [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) once you're past local setup.

---

## Prerequisites

| Tool            | Version | Why                                  |
|-----------------|---------|--------------------------------------|
| Docker + Compose| 24+     | Runs the whole stack                 |
| Anthropic API key | —     | Clip selection + metadata (Claude)   |
| (Local dev only) Python | 3.11 | Running services without Docker  |
| (Local dev only) FFmpeg | 6+  | All media operations             |
| (Optional) NVIDIA GPU + CUDA | — | Fast Whisper transcription      |

Get an API key at <https://console.anthropic.com>. Hardware: 8 GB RAM works
with `WHISPER_MODEL=small`; `large-v3` on CPU wants ~10 GB RAM and is slow —
use a GPU or a smaller model for long videos.

---

## Path A — Docker (recommended)

```bash
git clone <your-repo> ai-clip-farm && cd ai-clip-farm

cp .env.example .env
# Edit .env:
#   ANTHROPIC_API_KEY=sk-ant-...
#   POSTGRES_PASSWORD / N8N_* → change from defaults
#   WHISPER_MODEL=small        # start small, bump to large-v3 later

docker compose up -d --build
```

Verify:

```bash
curl http://localhost:8000/health      # {"status":"ok",...}
docker compose logs -f worker          # watch the pipeline run
```

Open <http://localhost:8000> and submit a video.

### GPU transcription (optional)

1. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
2. In `.env`: `WHISPER_DEVICE=cuda`, `WHISPER_COMPUTE_TYPE=float16`.
3. Give the `worker` service GPU access in `docker-compose.yml`:

   ```yaml
   worker:
     deploy:
       resources:
         reservations:
           devices:
             - driver: nvidia
               count: all
               capabilities: [gpu]
   ```
4. `docker compose up -d --build worker`.

### Production database migrations

The API auto-creates tables on startup (fine for dev). For controlled schema
changes, disable that and use Alembic:

```bash
docker compose exec api alembic upgrade head
```

---

## Path B — Local development (no Docker)

Useful for iterating on the pipeline.

```bash
# System deps
#   macOS:   brew install ffmpeg postgresql redis
#   Ubuntu:  sudo apt install ffmpeg libgl1 libglib2.0-0

python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start Postgres + Redis however you like (or just use the compose ones):
docker compose up -d postgres redis

# Point .env at localhost
#   DATABASE_URL=postgresql+psycopg://clipfarm:...@localhost:5432/clipfarm
#   REDIS_URL / CELERY_* = redis://localhost:6379/0
#   DATA_DIR=./data  INPUT_DIR=./data/input  WORK_DIR=./data/work  OUTPUT_DIR=./data/output

# Terminal 1 — API
uvicorn app.main:app --reload

# Terminal 2 — worker
celery -A app.workers.celery_app.celery_app worker --loglevel=INFO
```

Open <http://localhost:8000>.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `worker` exits / OOM during transcription | Use a smaller `WHISPER_MODEL`, or increase Docker memory. |
| Whisper re-downloads every run | Ensure the `whisper-cache` volume is mounted (it is by default). |
| Clips not tracking the speaker | Try `TRACKING_BACKEND=opencv`; `center` disables tracking. |
| Subtitles missing / wrong font | The image ships DejaVu/Liberation fonts; a custom font in `subtitles.py` must exist in the container. |
| Claude `refusal` / auth error | Check `ANTHROPIC_API_KEY`; see worker logs. |
| YouTube download fails | `yt-dlp` may need updating — rebuild the image, or the video is region/age restricted. |
| `400 source_ref must be an http(s) URL` / host not allowed | Only YouTube-family hosts are accepted by default (SSRF guard) — see `ALLOWED_SOURCE_HOSTS` in `.env`. |
| API returns `401 Missing or invalid API key` | Expected once `API_KEY` is set — pass `X-API-Key: <value>` on every `/api/*` request, or unset `API_KEY` for local dev. |
| API returns `429` | Rate limit hit (`RATE_LIMIT_PER_MINUTE`/`RATE_LIMIT_UPLOAD_PER_HOUR`) — raise the limit in `.env` or wait. |
| `docker compose` ignores CPU/memory limits | Requires Compose V2 (`docker compose`, not the legacy `docker-compose` v1 binary) — check with `docker compose version`. |
| Everything queued, nothing runs | Is the `worker` service up? `docker compose ps`. |

Watch queues in real time at <http://localhost:5555> (Flower).
