"""Unit tests for app.pipeline.metadata — hashtag normalization and the
Claude call shape. `claude_client.parse` is mocked."""

from __future__ import annotations

import pytest

from app.pipeline.metadata import ClipMetadata, generate


@pytest.mark.unit
def test_generate_normalizes_hashtags(mocker):
    fake = ClipMetadata(
        title="Amazing moment",
        hook="You won't believe this",
        description="A great clip.",
        hashtags=["#Funny", " comedy ", "ViralVideo", "", "  "],
    )
    mocker.patch("app.pipeline.metadata.claude_client.parse", return_value=fake)

    meta = generate("some transcript text", context_title="My Video")

    assert meta.hashtags == ["Funny", "comedy", "ViralVideo"]
    assert all("#" not in h for h in meta.hashtags)
    assert all(" " not in h for h in meta.hashtags)


@pytest.mark.unit
def test_generate_includes_context_title_in_prompt(mocker):
    fake = ClipMetadata(title="t", hook="h", description="d", hashtags=["x"])
    mock_parse = mocker.patch("app.pipeline.metadata.claude_client.parse", return_value=fake)

    generate("transcript", context_title="Podcast Episode 3")

    _, kwargs = mock_parse.call_args
    assert "Podcast Episode 3" in kwargs["user"]
    assert kwargs["purpose"] == "metadata"


@pytest.mark.unit
def test_generate_omits_context_header_when_no_title(mocker):
    fake = ClipMetadata(title="t", hook="h", description="d", hashtags=["x"])
    mock_parse = mocker.patch("app.pipeline.metadata.claude_client.parse", return_value=fake)

    generate("transcript only")

    _, kwargs = mock_parse.call_args
    assert kwargs["user"] == "CLIP TRANSCRIPT:\ntranscript only"
