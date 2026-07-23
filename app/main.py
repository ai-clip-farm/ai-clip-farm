"""FastAPI entrypoint: mounts the API, the static web UI, health checks and
metrics.

Schema management: in development, tables auto-create on startup for
zero-friction iteration. In production (`ENVIRONMENT=production`), this is
disabled — Alembic (`alembic upgrade head`) is the single source of truth for
schema, run explicitly in the deploy pipeline before the new API version
starts. Running both mechanisms against the same database is a classic way
to get schema drift that silently breaks migrations later.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

# Imported for its side effect only (registers SQLAlchemy models on
# Base.metadata before create_all() runs below) — never referenced by name.
# Aliased rather than `import app.models` specifically because that form
# binds the bare name `app` in this module's namespace, which then collides
# with the local `app = FastAPI(...)` assignment further down: mypy (correctly)
# treats that as reassigning a variable from Module type to FastAPI type.
from app import models as _app_models  # noqa: F401
from app.api.routes import router
from app.core.config import settings
from app.core.database import Base, check_db_connection, engine
from app.core.exceptions import ClipFarmError, ValidationError
from app.core.logging import logger
from app.core.security import limiter


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()

    if settings.environment != "production":
        Base.metadata.create_all(bind=engine)
        logger.info("Dev mode: tables ensured via create_all (use Alembic in production)")

    for warning in settings.ensure_runtime_ready():
        logger.warning("STARTUP WARNING: {}", warning)

    logger.info(
        "AI Clip Farm API started (env={}, model={}, auth_enabled={})",
        settings.environment,
        settings.claude_model,
        settings.auth_enabled,
    )
    yield
    logger.info("AI Clip Farm API shutting down")


app = FastAPI(
    title="AI Clip Farm",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
)

app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

WEB_DIR = Path(__file__).parent / "web"


# --- Middleware: request ID + timing (correlates logs across a single call) --


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.monotonic()
    with logger.contextualize(request_id=request_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = f"{(time.monotonic() - start) * 1000:.1f}"
    return response


# --- Exception handlers: never leak stack traces, always return clean JSON --


@app.exception_handler(ValidationError)
async def _validation_error_handler(_request: Request, exc: ValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(ClipFarmError)
async def _clipfarm_error_handler(request: Request, exc: ClipFarmError):
    logger.error("Unhandled ClipFarmError on {}: {}", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal processing error. See server logs for details."},
    )


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(_request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on {}", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# --- Health / readiness / metrics ---------------------------------------------


@app.get("/health")
def health() -> dict:
    """Liveness — the process is up and answering HTTP. No dependency
    checks: a load balancer should NOT restart this pod just because Redis
    had a blip. Use /health/ready for that."""
    return {"status": "ok", "version": app.version}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """Readiness — are our actual dependencies reachable? Used by
    orchestrators (k8s readinessProbe, Docker healthcheck) to decide whether
    to route traffic here."""
    checks = {"database": check_db_connection()}
    healthy = all(checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if healthy else "not_ready", "checks": checks},
    )


if settings.metrics_enabled:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# Serve the SPA assets (index.html + app.js + styles) at /static.
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
