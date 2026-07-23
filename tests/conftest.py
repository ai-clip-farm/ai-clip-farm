"""Shared pytest fixtures.

Critical ordering constraint: `app.core.config.settings` and
`app.core.database.engine` are both created once, at import time (module-level
singletons — see `app/core/config.py` and `app/core/database.py`). Pytest
loads `conftest.py` before collecting any test module, so setting environment
variables here — at module scope, before any `from app...` import — is what
makes those singletons pick up test-safe values (a temp-file SQLite DB, a
throwaway API key, auth disabled) instead of whatever's in a developer's local
`.env`.

If a test file does `from app.core.config import settings` before this
module has run, the ordering guarantee breaks — always go through the
fixtures below rather than importing `app.*` at module scope in test files.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="clipfarm-test-")
_db_path = Path(_tmp_dir) / "test.db"

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("API_KEY", "")  # auth disabled by default in tests
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_path}")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/15")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/15")
os.environ.setdefault("DATA_DIR", str(Path(_tmp_dir) / "data"))
os.environ.setdefault("INPUT_DIR", str(Path(_tmp_dir) / "data" / "input"))
os.environ.setdefault("WORK_DIR", str(Path(_tmp_dir) / "data" / "work"))
os.environ.setdefault("OUTPUT_DIR", str(Path(_tmp_dir) / "data" / "output"))
os.environ.setdefault("METRICS_ENABLED", "false")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")
os.environ.setdefault("FLOWER_PASSWORD", "test")

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_settings():
    """`settings` is a process-wide singleton (app/core/config.py), and many
    tests deliberately mutate a field on it directly (e.g.
    `test_settings.max_clips_per_video = 1`) to exercise a specific config
    path — that's the right way to test config-driven behavior, but without
    this fixture the mutation would leak into every test that runs
    afterward in the same pytest session (pytest doesn't restart the
    process between tests), silently changing their effective config based
    on file/test collection order. Snapshot every field before each test and
    restore it after, so no test needs its own try/finally revert."""
    original = settings.model_dump()
    yield
    for key, value in original.items():
        setattr(settings, key, value)


@pytest.fixture(autouse=True)
def _clean_database():
    """Fresh schema for every test — cheap against a local SQLite file and
    keeps tests independent (no ordering-dependent state leaking between
    them, which is what usually rots a test suite's reliability over time)."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_settings():
    return settings


@pytest.fixture
def sample_transcript() -> dict:
    """A small, hand-built transcript with word-level timestamps — enough to
    exercise snapping, ASS subtitle building, and clip-window extraction
    without needing a real audio file or Whisper."""
    words = [
        {"start": 0.0, "end": 0.3, "word": "Hello"},
        {"start": 0.3, "end": 0.5, "word": "world,"},
        {"start": 0.5, "end": 0.9, "word": "this"},
        {"start": 0.9, "end": 1.0, "word": "is"},
        {"start": 1.0, "end": 1.4, "word": "a"},
        {"start": 1.4, "end": 2.0, "word": "test."},
        {"start": 5.0, "end": 5.4, "word": "Second"},
        {"start": 5.4, "end": 5.9, "word": "segment"},
        {"start": 5.9, "end": 6.2, "word": "here."},
    ]
    return {
        "language": "en",
        "duration": 10.0,
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "Hello world, this is a test.", "words": words[:6]},
            {"start": 5.0, "end": 6.2, "text": "Second segment here.", "words": words[6:]},
        ],
    }


@pytest.fixture
def anthropic_text_response():
    """Build a fake object shaped like an `anthropic.types.Message` response
    with a single text block — enough for `claude_client.parse` to consume
    without importing the real SDK's response classes."""

    def _make(json_text: str, stop_reason: str = "end_turn", input_tokens=100, output_tokens=50):
        class _Block:
            type = "text"
            text = json_text

        class _Usage:
            def __init__(self):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens

        class _Resp:
            def __init__(self):
                self.content = [_Block()]
                self.stop_reason = stop_reason
                self.stop_details = None
                self.usage = _Usage()

        return _Resp()

    return _make
