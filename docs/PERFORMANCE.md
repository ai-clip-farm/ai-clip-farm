# Performance Report

## What was and wasn't verified

**Honesty check first, because it matters for how you read the rest of this
document.** This entire Phase 2 hardening pass was done in an environment
with no Python interpreter, no Docker, and no GPU available — every
optimization below is a well-founded engineering change (each one addresses
a specific, named bottleneck), but **none of the numbers in this document
are measured benchmarks from this codebase.** They are:

- Either well-established properties of the technique itself (e.g. "loading
  a 1.5GB Whisper model takes 10-60s" is a widely-documented faster-whisper
  characteristic, not something specific to this code), or
- Directional estimates ("roughly Nx") clearly labeled as such.

**Before trusting a number here for capacity planning, reproduce it against
your own hardware and your own representative videos** using the commands
in § Benchmarking your own deployment below. Treat this document as "what
was optimized and why," not "here are the SLAs."

---

## Optimizations applied, and the bottleneck each one targets

### 1. Whisper model caching (`app/pipeline/transcribe.py`)

**Before:** `WhisperModel(...)` was constructed fresh on every single
transcription call.

**Problem:** Loading `large-v3` involves deserializing ~1.5GB of weights
into memory/CTranslate2's runtime — a fixed cost paid on *every video*,
regardless of the video's length.

**Fix:** A process-level cache (`_model_cache`, keyed by
`(model, device, compute_type)`) loads the model at most once per worker
process; `worker_max_tasks_per_child=20` in `celery_app.py` still recycles
the process periodically to bound memory growth, so the cache isn't
permanent — it's amortized across a batch of tasks, not held forever.

**Expected impact:** Eliminates the model-load cost on every transcription
after the first one in a given worker process. The magnitude depends
entirely on your `WHISPER_MODEL` size and disk/CPU speed — measure it with
the harness in § Benchmarking.

### 2. Face-detection frame striding (`app/pipeline/reframe.py`, `FACE_DETECT_STRIDE`)

**Before:** MediaPipe face detection ran on every single frame.

**Problem:** MediaPipe inference is the dominant per-frame cost in the
reframe stage; the output trajectory is already exponentially smoothed
(`_smooth()`), so a fresh detection every frame buys negligible additional
smoothness over detecting every 3rd-5th frame and interpolating between hits.

**Fix:** `_track_strided()` detects every `FACE_DETECT_STRIDE` frames
(default 3) and reuses the last detection for frames in between, before the
existing EMA smoothing pass.

**Expected impact:** Roughly `FACE_DETECT_STRIDE`x fewer MediaPipe inference
calls for the reframe stage specifically (not the whole pipeline). At the
default of 3, that's a directional ~3x reduction in the stage's own
inference cost — actual wall-clock savings depend on how much of the stage's
total time MediaPipe inference represents versus frame I/O/resize/encode.

### 3. Hardware-accelerated encoding (`FFMPEG_HWACCEL=auto`, `app/pipeline/ffmpeg_utils.py`)

**Before:** Every ffmpeg encode used software `libx264`.

**Fix:** `auto` probes for an NVIDIA GPU (`nvidia-smi`) and switches to
`h264_nvenc`; `_run_encode()` transparently retries with `libx264` if the
hardware encoder fails mid-job (driver mismatch, VRAM exhaustion, etc.), so
enabling this never turns a working pipeline into a failing one.

**Expected impact:** NVENC vs. libx264 at comparable quality settings is a
well-documented multi-x speedup for H.264 encoding specifically (the ffmpeg
project's own benchmarks consistently show this), but the *end-to-end* clip
render time also includes cut/reframe/subtitle-burn stages that aren't
GPU-accelerated by this change — measure the whole pipeline, not just the
encode step, before sizing a GPU purchase around this number alone.

### 4. Resource-leak fix in `reframe.py`

**Before:** `cap.release()`/`writer.release()` only ran on the success
path; an exception mid-loop (corrupt frame, disk full) leaked the OpenCV
capture/writer handles.

**Fix:** Both are released in `finally` blocks now.

**Why this is a performance fix, not just a correctness one:** under
`worker_max_tasks_per_child=20` recycling, a slow leak might never surface
as an obvious crash — it manifests as gradually increasing memory/file-
descriptor pressure across a day of unattended batch processing, showing up
as unrelated-looking failures hours later. Fixing the leak removes that
failure mode entirely rather than just making it happen less often.

### 5. Work-directory cleanup (`app/core/cleanup.py`)

**Before:** Nothing under `WORK_DIR` was ever deleted.

**Problem:** Every video accumulates a downloaded source, extracted audio,
and per-clip intermediates (cut/reframed/subtitled versions before the
final burn). At "hundreds of videos/day," this is unbounded disk growth
that eventually stops the pipeline dead with `ENOSPC`, not a speed issue —
but a full disk is the single most common cause of an "it just stopped
processing" production incident for exactly this kind of workload.

**Fix:** Per-clip intermediates are deleted immediately after a successful
render; the whole per-video workspace (source download + extracted audio)
is deleted once every clip has been attempted (`finalize_video`); an hourly
Celery Beat safety-net purges anything older than
`WORK_DIR_RETENTION_HOURS` regardless of why normal cleanup didn't run.

### 6. Parallel clip rendering (already present in Phase 1, unchanged)

`workers/tasks.py`'s `chord(render_clip.s(cid) for cid in clip_ids, ...)`
fans the 10-15 clips-per-video render step across the whole Celery worker
pool concurrently rather than one at a time. This is the single biggest
lever for *videos/day* throughput, independent of any single-clip
optimization above — see § Scaling below.

---

## Benchmarking your own deployment

Run these against your actual hardware before making a capacity decision.

```bash
# 1. Whisper: cold vs. warm transcription time for a representative video
docker compose exec worker python -c "
import time
from app.pipeline import transcribe
from pathlib import Path
t0 = time.monotonic()
transcribe.transcribe('/data/input/sample.mp4', Path('/tmp'))
print(f'cold: {time.monotonic() - t0:.1f}s')
t0 = time.monotonic()
transcribe.transcribe('/data/input/sample.mp4', Path('/tmp'))
print(f'warm (cached model): {time.monotonic() - t0:.1f}s')
"

# 2. Reframe stage at different FACE_DETECT_STRIDE values
for stride in 1 3 5 10; do
  docker compose exec -e FACE_DETECT_STRIDE=$stride worker python -c "
import time
from app.pipeline import reframe
from pathlib import Path
t0 = time.monotonic()
reframe.reframe('/data/work/sample_cut.mp4', Path('/tmp/out.mp4'), Path('/tmp'))
print(f'stride=$stride: {time.monotonic() - t0:.1f}s')
"
done

# 3. NVENC vs libx264 (only meaningful on a GPU host)
time docker compose exec -e FFMPEG_HWACCEL=nvenc worker \
  ffmpeg -y -i /data/input/sample.mp4 -c:v h264_nvenc -cq 20 /tmp/out_gpu.mp4
time docker compose exec -e FFMPEG_HWACCEL=none worker \
  ffmpeg -y -i /data/input/sample.mp4 -c:v libx264 -preset veryfast -crf 20 /tmp/out_cpu.mp4

# 4. Prometheus stage-duration histogram, once the stack has processed
#    real traffic — this is the authoritative source of truth, since it's
#    measuring *your* videos on *your* hardware, not a synthetic benchmark:
curl -s localhost:9100/metrics | grep clipfarm_stage_duration_seconds
```

---

## Disk sizing

Rough per-video disk footprint (varies with source resolution/length):

| Artifact | Approx. size | Lifetime |
|---|---|---|
| Downloaded source (1080p, per minute) | ~8-15 MB/min | Deleted at `finalize_video` |
| Extracted audio (16kHz mono WAV) | ~2 MB/min | Deleted at `finalize_video` |
| Per-clip intermediates (cut/framed/subs) | ~5-10 MB per clip, ×3 stages | Deleted immediately after that clip renders |
| Final output (`clip.mp4` + thumbnail + metadata.json) | ~5-15 MB per clip | **Kept indefinitely** — this is the product |

At 15 videos/day × 12 clips average × 10MB final output ≈ **1.8 GB/day** of
permanent output growth. Plan `OUTPUT_DIR` storage (and its backup target)
around your actual retention policy, not the working-directory numbers
above, which are transient by design.

---

## Scaling to hundreds of videos/day

1. **Add worker replicas**, not bigger ones, first:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --scale worker=4
   ```
   `task_acks_late=True` + `worker_prefetch_multiplier=1` (celery_app.py)
   mean a crashed worker's in-flight task simply gets redelivered to another
   replica — safe to scale this way without special coordination.
2. **Separate queues under real load.** `celery_app.py` already routes
   `pipeline` (ingest/transcribe/render — CPU/GPU-heavy) separately from
   `maintenance` (cleanup/reports — cheap). Once you're running enough
   volume that cleanup tasks visibly compete with renders for a worker
   slot, run maintenance on its own dedicated worker:
   ```bash
   celery -A app.workers.celery_app.celery_app worker -Q maintenance --concurrency=1
   ```
3. **GPU workers for transcription specifically.** Whisper is the single
   most CPU-intensive stage per minute of source video; a worker pool split
   into "GPU workers running WHISPER_DEVICE=cuda" and "CPU workers handling
   cut/reframe/subtitle" lets you right-size each pool's hardware
   independently rather than provisioning every worker for the worst case.
4. **Watch `clipfarm_videos_in_flight`** (Prometheus gauge, `app/core/metrics.py`)
   against your worker concurrency — if it's consistently pinned at your
   concurrency ceiling, you're queue-bound and need more workers, not faster
   ones.
5. **Postgres connection pool.** `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` (default
   10/20) must scale with total worker process count — each Celery prefork
   child holds its own pool. At `worker=4 --concurrency=4` (16 processes),
   raise `DB_POOL_SIZE` accordingly or you'll see connection-wait latency
   under load, visible as `check_db_connection()` readiness-probe slowness
   even though nothing is actually broken.
