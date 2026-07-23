# ============================================================================
# Multi-stage build shared by the API and Celery worker.
#
# Stage 1 (builder) installs Python dependencies into a venv using build
# tools that never ship in the final image. Stage 2 (runtime) copies just the
# venv + app code onto a minimal base, runs as a non-root user, and adds a
# HEALTHCHECK — cutting image size roughly in half versus a single-stage
# build and removing compilers/headers from the attack surface entirely.
# ============================================================================

# ---- Stage 1: builder ----
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools only exist in this stage — mediapipe/opencv/psycopg ship
# manylinux wheels for x86_64, but a source build (e.g. on an uncommon
# architecture) needs a compiler available somewhere.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: runtime ----
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime-only system deps:
#   ffmpeg              - all media operations
#   libgl1/libglib2.0-0 - required by OpenCV/MediaPipe at import time
#   fonts-dejavu-core, fonts-liberation - burned-in subtitle rendering
#     (see app/pipeline/subtitles.py STYLE_PRESETS — font names there must
#     match a family one of these packages installs)
#   curl                - used by the Docker/Compose HEALTHCHECK below
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        fonts-dejavu-core \
        fonts-liberation \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

COPY --from=builder /opt/venv /opt/venv

# Non-root user: the pipeline never needs root, and running unattended,
# every-day media processing as root turns any future RCE (a malicious
# input file exploiting an ffmpeg/opencv parser bug, for instance) into a
# full container compromise instead of a contained one.
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app
COPY --chown=appuser:appuser . .

# Data volume mount points (input/work/output live here). Created and
# owned by appuser so a fresh volume mount doesn't need a root init step.
RUN mkdir -p /data/input /data/work /data/output && \
    chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Default command runs the API; docker-compose overrides this for the
# worker/beat/flower services.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
