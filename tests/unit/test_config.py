"""Unit tests for app.core.config.Settings — the fail-fast validation that
should stop a misconfigured production deploy before it ever accepts traffic.

These tests construct `Settings(...)` directly (bypassing the cached
`get_settings()` singleton) so they can exercise different configurations
without disturbing the process-wide settings other tests rely on.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, generate_api_key


@pytest.mark.unit
class TestProductionInvariants:
    def test_production_requires_anthropic_key(self):
        with pytest.raises(PydanticValidationError, match="ANTHROPIC_API_KEY"):
            Settings(
                environment="production",
                anthropic_api_key="",
                api_key="something",
                cors_origins="https://example.com",
            )

    def test_production_requires_api_key(self):
        with pytest.raises(PydanticValidationError, match="API_KEY must be set"):
            Settings(
                environment="production",
                anthropic_api_key="sk-ant-xxx",
                api_key="",
                cors_origins="https://example.com",
            )

    def test_production_rejects_wildcard_cors(self):
        with pytest.raises(PydanticValidationError, match="CORS_ORIGINS"):
            Settings(
                environment="production",
                anthropic_api_key="sk-ant-xxx",
                api_key="something",
                cors_origins="*",
            )

    def test_valid_production_config_passes(self):
        s = Settings(
            environment="production",
            anthropic_api_key="sk-ant-xxx",
            api_key="a-real-key",
            cors_origins="https://example.com",
        )
        assert s.environment == "production"

    def test_development_allows_missing_keys(self):
        s = Settings(environment="development", anthropic_api_key="", api_key="")
        assert s.environment == "development"


@pytest.mark.unit
class TestLiteralValidation:
    def test_rejects_invalid_claude_effort(self):
        with pytest.raises(PydanticValidationError):
            Settings(claude_effort="super-duper-high")

    def test_rejects_invalid_whisper_device(self):
        with pytest.raises(PydanticValidationError):
            Settings(whisper_device="gpu")  # correct value is "cuda"

    def test_rejects_invalid_tracking_backend(self):
        with pytest.raises(PydanticValidationError):
            Settings(tracking_backend="yolo")

    def test_accepts_valid_values(self):
        s = Settings(
            claude_effort="xhigh", whisper_device="cpu", tracking_backend="opencv"
        )
        assert s.claude_effort == "xhigh"


@pytest.mark.unit
class TestDerivedProperties:
    def test_cors_origin_list_splits_and_strips(self):
        s = Settings(cors_origins=" http://a.com , http://b.com ")
        assert s.cors_origin_list == ["http://a.com", "http://b.com"]

    def test_allowed_video_extension_set_lowercases(self):
        s = Settings(allowed_video_extensions=".MP4,.MOV")
        assert s.allowed_video_extension_set == {".mp4", ".mov"}

    def test_auth_enabled_reflects_api_key(self):
        assert Settings(api_key="").auth_enabled is False
        assert Settings(api_key="secret").auth_enabled is True

    def test_ensure_runtime_ready_warns_on_empty_anthropic_key(self):
        s = Settings(anthropic_api_key="", api_key="x")
        warnings = s.ensure_runtime_ready()
        assert any("ANTHROPIC_API_KEY" in w for w in warnings)

    def test_ensure_runtime_ready_warns_on_disabled_auth(self):
        s = Settings(anthropic_api_key="sk-x", api_key="")
        warnings = s.ensure_runtime_ready()
        assert any("UNAUTHENTICATED" in w for w in warnings)

    def test_ensure_runtime_ready_clean_when_fully_configured(self):
        s = Settings(anthropic_api_key="sk-x", api_key="secret")
        assert s.ensure_runtime_ready() == []


@pytest.mark.unit
def test_generate_api_key_is_random_and_url_safe():
    a, b = generate_api_key(), generate_api_key()
    assert a != b
    assert len(a) > 20
