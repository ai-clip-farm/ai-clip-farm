"""Unit tests for app.core.security.require_api_key — the auth dependency
that closes the "anyone on the network can submit jobs" hole. asyncio_mode
is "auto" (see pyproject.toml) so plain `async def test_...` works without
an explicit @pytest.mark.asyncio decorator.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.security import require_api_key


@pytest.mark.unit
async def test_auth_disabled_when_no_api_key_configured(test_settings):
    test_settings.api_key = ""
    await require_api_key(x_api_key=None, authorization=None)  # must not raise


@pytest.mark.unit
async def test_rejects_missing_key_when_enabled(test_settings):
    test_settings.api_key = "secret-key"
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key=None, authorization=None)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
async def test_rejects_wrong_key(test_settings):
    test_settings.api_key = "secret-key"
    with pytest.raises(HTTPException) as exc_info:
        await require_api_key(x_api_key="wrong-key", authorization=None)
    assert exc_info.value.status_code == 401


@pytest.mark.unit
async def test_accepts_correct_key_via_x_api_key_header(test_settings):
    test_settings.api_key = "secret-key"
    await require_api_key(x_api_key="secret-key", authorization=None)  # must not raise


@pytest.mark.unit
async def test_accepts_correct_key_via_bearer_token(test_settings):
    test_settings.api_key = "secret-key"
    await require_api_key(x_api_key=None, authorization="Bearer secret-key")  # must not raise


@pytest.mark.unit
async def test_x_api_key_takes_precedence_over_bearer(test_settings):
    test_settings.api_key = "secret-key"
    # If X-API-Key is present it wins even if Authorization is also sent —
    # must not raise since X-API-Key is correct regardless of the bearer value.
    await require_api_key(x_api_key="secret-key", authorization="Bearer garbage")
