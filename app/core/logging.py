"""Loguru setup — imported once (for its side effect) before any other module
logs. Supports two modes:

  LOG_JSON=false (default, dev) — human-readable colored console output.
  LOG_JSON=true  (production)   — one JSON object per line, ready for
                                   Loki/ELK/CloudWatch ingestion.

`bind(request_id=...)` (set by the API's request-ID middleware) and any other
`extra` fields are automatically included in JSON mode so a single request
can be traced across the API and, via the Celery task ID, into worker logs.
"""
from __future__ import annotations

import sys

from loguru import logger

from app.core.config import settings


def _json_sink(message) -> None:
    import json

    record = message.record
    payload = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
    }
    if record["exception"]:
        payload["exception"] = str(record["exception"])
    extra = {k: v for k, v in record["extra"].items() if k not in payload}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, default=str), file=sys.stderr)


def configure_logging() -> None:
    logger.remove()
    if settings.log_json:
        logger.add(_json_sink, level=settings.log_level, backtrace=True, diagnose=False)
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan> - <level>{message}</level>"
            ),
            backtrace=False,
            diagnose=False,
        )


configure_logging()

__all__ = ["logger"]
