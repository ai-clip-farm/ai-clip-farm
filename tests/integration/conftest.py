"""Integration-test-only fixtures.

Rate limiting is disabled for these tests: slowapi's `Limiter` is backed by
Redis (`app.core.security.limiter`), and CI/local test runs shouldn't have to
depend on a live Redis instance just to exercise route logic. Rate limiting
itself is covered conceptually by `tests/unit/test_security.py`'s auth tests
and should be smoke-tested manually (or in a dedicated environment with real
Redis) before a production deploy — see docs/SECURITY_CHECKLIST.md.
"""
from __future__ import annotations

import pytest

from app.core.security import limiter


@pytest.fixture(autouse=True)
def _disable_rate_limiting():
    limiter.enabled = False
    yield
    limiter.enabled = True
