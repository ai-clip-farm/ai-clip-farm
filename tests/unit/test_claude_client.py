"""Unit tests for app.pipeline.claude_client — the structured-output call
shape, refusal handling, and error mapping. `_create` (the tenacity-wrapped
API call) is mocked directly so these tests never touch the network.
"""
from __future__ import annotations

import anthropic
import pytest
from pydantic import BaseModel

from app.core.exceptions import AnalysisError, ClaudeRefusalError
from app.pipeline import claude_client


class _Schema(BaseModel):
    name: str
    count: int


@pytest.mark.unit
def test_parse_returns_validated_model(mocker, anthropic_text_response):
    resp = anthropic_text_response('{"name": "clip", "count": 3}')
    mocker.patch.object(claude_client, "_create", return_value=resp)

    result = claude_client.parse(system="sys", user="usr", schema=_Schema, purpose="test")

    assert isinstance(result, _Schema)
    assert result.name == "clip"
    assert result.count == 3


@pytest.mark.unit
def test_parse_raises_on_refusal(mocker, anthropic_text_response):
    resp = anthropic_text_response('{"name": "x", "count": 1}', stop_reason="refusal")
    mocker.patch.object(claude_client, "_create", return_value=resp)

    with pytest.raises(ClaudeRefusalError):
        claude_client.parse(system="sys", user="usr", schema=_Schema)


@pytest.mark.unit
def test_parse_raises_on_invalid_json(mocker, anthropic_text_response):
    resp = anthropic_text_response("not json at all")
    mocker.patch.object(claude_client, "_create", return_value=resp)

    with pytest.raises(AnalysisError, match="schema validation"):
        claude_client.parse(system="sys", user="usr", schema=_Schema)


@pytest.mark.unit
def test_parse_raises_on_schema_mismatch(mocker, anthropic_text_response):
    resp = anthropic_text_response('{"wrong_field": true}')
    mocker.patch.object(claude_client, "_create", return_value=resp)

    with pytest.raises(AnalysisError):
        claude_client.parse(system="sys", user="usr", schema=_Schema)


@pytest.mark.unit
def test_parse_raises_on_empty_content(mocker):
    class _EmptyResp:
        content = []
        stop_reason = "end_turn"
        stop_details = None

        class usage:
            input_tokens = 10
            output_tokens = 0

    mocker.patch.object(claude_client, "_create", return_value=_EmptyResp())

    with pytest.raises(AnalysisError, match="no text content"):
        claude_client.parse(system="sys", user="usr", schema=_Schema)


@pytest.mark.unit
def test_parse_wraps_api_status_error(mocker):
    # Bypass anthropic.APIStatusError's real __init__ (its exact required
    # kwargs vary across SDK versions and aren't worth coupling this test
    # to) — we only need a real instance for the `except APIStatusError`
    # isinstance check to fire, with `.status_code`/`.message` set for the
    # f-string in claude_client.parse to read.
    err = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    err.status_code = 400
    err.message = "bad request"
    mocker.patch.object(claude_client, "_create", side_effect=err)

    with pytest.raises(AnalysisError, match="400"):
        claude_client.parse(system="sys", user="usr", schema=_Schema)


@pytest.mark.unit
def test_client_requires_api_key(mocker):
    mocker.patch("app.pipeline.claude_client.settings.anthropic_api_key", "")
    mocker.patch.object(claude_client, "_client", None)

    with pytest.raises(AnalysisError, match="ANTHROPIC_API_KEY"):
        claude_client.client()
