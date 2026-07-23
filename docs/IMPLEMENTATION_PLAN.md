# Step-by-Step Implementation Plan

This is the order the project was built in — follow it to extend, review, or
rebuild any part. Each phase is independently testable.

## Phase 0 — Foundation ✅
- `.env.example`, `requirements.txt`, `app/core/config.py` (single settings
  source), logging, `docker-compose.yml`, `Dockerfile`.
- **Test:** `docker compose up postgres redis`, `curl /health`.

## Phase 1 — Data model ✅
- `app/models/schema.py` (Video / Clip / Job) + Alembic migration.
- **Test:** tables auto-create on API startup; `alembic upgrade head` in prod.

## Phase 2 — Ingest ✅
- `pipeline/ingest.py`: yt-dlp for URLs, copy for local/uploaded files;
  `ffmpeg_utils.py` for probe/duration.
- **Test:** submit a URL, confirm `data/work/<id>/source.mp4` appears.

## Phase 3 — Transcription ✅
- `pipeline/transcribe.py`: faster-whisper with word timestamps + VAD.
- **Test:** transcript JSON stored on the video; segments have `words[]`.

## Phase 4 — Clip selection (Claude) ✅
- `pipeline/claude_client.py` (structured output + retries),
  `pipeline/analyze.py` (prompt, schema, word-snapping, duration clamping).
- **Test:** 10–15 ranked `Clip` rows created with scores + reasons.

## Phase 5 — Cut & reframe ✅
- `pipeline/cut.py` (FFmpeg segment), `pipeline/reframe.py` (9:16 + face
  tracking + smoothing + audio mux).
- **Test:** a `framed.mp4` at 1080×1920 that follows the speaker.

## Phase 6 — Animated subtitles ✅
- `pipeline/subtitles.py`: ASS generation with karaoke highlight + pop-in;
  burn-in via FFmpeg. Style presets are data-driven.
- **Test:** captions appear word-synced on the clip.

## Phase 7 — Metadata (Claude) ✅
- `pipeline/metadata.py`: title / hook / description / hashtags per clip.
- **Test:** `metadata.json` written next to each `clip.mp4`.

## Phase 8 — Orchestration & async ✅
- `pipeline/orchestrator.py` chains stages + persists state;
  `workers/tasks.py` (`process_video` → `chord(render_clip…)` → `finalize`).
- **Test:** whole run completes; clips render in parallel.

## Phase 9 — API & Web UI ✅
- `api/routes.py` + `api/schemas.py`; `app/web/` dashboard (submit, live
  progress, clip grid, preview/download modal).
- **Test:** end-to-end from the browser.

## Phase 10 — Automation ✅
- n8n workflows (webhook submit+notify, nightly batch); Flower monitoring.

## Phase 11 — Hardening (recommended next)
- Add `pytest` unit tests per stage with a short sample clip fixture.
- Add auth (API key / OAuth) before exposing beyond localhost.
- Structured cost logging (Claude token usage per video).
- Retry/backoff tuning and dead-letter handling for `render_clip`.

---

# Future Improvements

**Editing quality**
- Auto **zoom/punch-in** on emphasis and **B-roll** insertion on keywords.
- Multi-speaker **diarization** → switch the crop between speakers in a debate.
- **Silence/filler-word removal** (jump cuts) for tighter pacing.
- Music bed + auto-ducking; loudness normalization (EBU R128).
- Per-platform variants (9:16, 1:1, 4:5) rendered in one pass.

**Intelligence**
- A **virality-predictor** model fine-tuned on your own performance data to
  re-rank Claude's candidates.
- **Local-LLM fallback** (e.g. Llama via Ollama) for metadata when fully
  offline; keep Claude as the quality default.
- Vision pass over sampled frames so selection considers *visual* moments, not
  just transcript.
- Thumbnail A/B generation + on-frame text.

**Distribution**
- Direct publishing connectors: TikTok, YouTube Shorts, Instagram Reels APIs,
  with scheduling.
- Webhook/notification on completion (already stubbed in the n8n workflow).

**Ops & scale**
- GPU worker pool with autoscaling; separate transcription and render queues.
- S3/MinIO storage backend behind the same `output_path` abstraction.
- Cost dashboard + per-video budget caps (Claude `task_budget`).
- Full test suite + CI, and Alembic-only schema management in production.
