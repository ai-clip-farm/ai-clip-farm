"""Shared Claude wrapper.

Centralises model, effort, thinking, timeouts, retries, metrics and
structured-output parsing so the analyze/metadata stages stay declarative.

Deliberately does **not** use `messages.parse(output_format=schema)`: that
convenience path and an explicit `output_config={"effort": ...}` are not
documented as combinable, and getting it wrong fails silently deep inside a
Celery task. Instead we build `output_config` ourselves (format *and*
effort in one place), call `messages.create()`, and validate the returned
JSON text against the Pydantic schema — the fully-documented, unambiguous
path, at the cost of one extra `model_validate_json` call.
"""

from __future__ import annotations

import json
from typing import TypeVar

import anthropic
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.exceptions import AnalysisError, ClaudeRefusalError
from app.core.logging import logger
from app.core.metrics import CLAUDE_REQUESTS, CLAUDE_TOKENS

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None

# Transient errors worth retrying with backoff. 400s (bad schema/prompt) and
# auth errors are deliberately excluded — retrying those just burns time and
# quota for an error that will never succeed.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise AnalysisError("ANTHROPIC_API_KEY is not set — cannot call the Claude API")
        _client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.claude_timeout_seconds,
            max_retries=0,  # tenacity below owns retries so we control backoff/metrics
        )
    return _client


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    before_sleep=before_sleep_log(logger, "WARNING"),  # type: ignore[arg-type]
    reraise=True,
)
def _create(*, system: str, user: str, output_config: dict, max_tokens: int):
    # Plain dicts here (not the SDK's OutputConfigParam/ThinkingConfigAdaptiveParam
    # TypedDicts) match the documented API request shape exactly and work
    # correctly at runtime; mypy can't verify a plain dict against the
    # overloaded signature's specific TypedDict params, hence the ignore.
    return client().messages.create(  # type: ignore[call-overload]
        model=settings.claude_model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config=output_config,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


def parse(
    *,
    system: str,
    user: str,
    schema: type[T],
    max_tokens: int = 8000,
    purpose: str = "generic",
) -> T:
    """Send one request and return a validated Pydantic model."""
    output_config = {
        "effort": settings.claude_effort,
        "format": {
            "type": "json_schema",
            "schema": schema.model_json_schema(),
        },
    }

    logger.debug("Claude request ({} chars user, purpose={})", len(user), purpose)
    try:
        resp = _create(system=system, user=user, output_config=output_config, max_tokens=max_tokens)
    except _RETRYABLE as e:
        CLAUDE_REQUESTS.labels(purpose=purpose, outcome="error").inc()
        raise AnalysisError(f"Claude request failed after retries: {e}") from e
    except anthropic.APIStatusError as e:
        CLAUDE_REQUESTS.labels(purpose=purpose, outcome="error").inc()
        raise AnalysisError(f"Claude returned {e.status_code}: {e.message}") from e

    CLAUDE_TOKENS.labels(purpose=purpose, kind="input").inc(resp.usage.input_tokens)
    CLAUDE_TOKENS.labels(purpose=purpose, kind="output").inc(resp.usage.output_tokens)

    if resp.stop_reason == "refusal":
        CLAUDE_REQUESTS.labels(purpose=purpose, outcome="refusal").inc()
        raise ClaudeRefusalError(f"Claude refused the request: {resp.stop_details}")

    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        CLAUDE_REQUESTS.labels(purpose=purpose, outcome="empty").inc()
        raise AnalysisError(f"Claude returned no text content (stop_reason={resp.stop_reason})")

    try:
        data = json.loads(text)
        validated = schema.model_validate(data)
    except (json.JSONDecodeError, PydanticValidationError) as e:
        CLAUDE_REQUESTS.labels(purpose=purpose, outcome="invalid_schema").inc()
        raise AnalysisError(f"Claude response failed schema validation: {e}") from e

    CLAUDE_REQUESTS.labels(purpose=purpose, outcome="success").inc()
    return validated
