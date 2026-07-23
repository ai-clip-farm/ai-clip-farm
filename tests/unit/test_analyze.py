"""Unit tests for app.pipeline.analyze — clip-boundary snapping, duration
clamping, category filtering, and ranking. `claude_client.parse` is mocked so
these tests exercise only our own post-processing logic, not the Claude API.
"""

from __future__ import annotations

import pytest

from app.pipeline.analyze import AnalysisResult, ClipCandidate, _post_process, analyze


def _candidate(**overrides) -> ClipCandidate:
    defaults = {
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "score": 80.0,
        "title_hint": "hint",
        "reason": "funny moment",
        "categories": ["funny"],
        "transcript_text": "Hello world, this is a test.",
    }
    defaults.update(overrides)
    return ClipCandidate(**defaults)


@pytest.mark.unit
class TestPostProcess:
    """`sample_transcript`'s clips (0.0-2.0s, 5.0-6.2s) are shorter than the
    real MIN_CLIP_SECONDS default (15s) — realistic for a genuine clip, but
    these tests are specifically about snapping/clamping/category-filtering
    in isolation, not the minimum-duration filter (which has its own
    dedicated test below), so they relax it to 0 to avoid every candidate
    being rejected before the behavior under test even runs.
    """

    def test_snaps_start_and_end_to_word_boundaries(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        # 0.1 and 1.9 don't land exactly on a word edge; nearest edges are
        # 0.0 (start of "Hello") and 2.0 (end of "test.").
        candidates = [_candidate(start_seconds=0.1, end_seconds=1.9)]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].start_seconds == 0.0
        assert out[0].end_seconds == 2.0

    def test_drops_candidate_shorter_than_minimum_after_snapping(
        self, sample_transcript, test_settings
    ):
        candidates = [_candidate(start_seconds=0.0, end_seconds=0.3)]
        out = _post_process(candidates, sample_transcript)
        assert out == []

    def test_clamps_candidate_longer_than_maximum(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        test_settings.max_clip_seconds = 1.0
        candidates = [_candidate(start_seconds=0.0, end_seconds=2.0)]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].end_seconds - out[0].start_seconds <= 1.0 + 1e-9

    def test_clamps_timestamps_to_transcript_duration(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        candidates = [_candidate(start_seconds=-5.0, end_seconds=999.0)]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].start_seconds >= 0.0
        assert out[0].end_seconds <= sample_transcript["duration"]

    def test_drops_candidate_with_end_before_start(self, sample_transcript):
        candidates = [_candidate(start_seconds=5.0, end_seconds=1.0)]
        out = _post_process(candidates, sample_transcript)
        assert out == []

    def test_filters_invalid_categories(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        candidates = [_candidate(categories=["funny", "made_up_category"])]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].categories == ["funny"]

    def test_defaults_to_viral_when_all_categories_invalid(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        candidates = [_candidate(categories=["nonsense"])]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].categories == ["viral"]

    def test_ranks_best_score_first(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        candidates = [
            _candidate(start_seconds=0.0, end_seconds=2.0, score=40.0),
            _candidate(start_seconds=5.0, end_seconds=6.2, score=95.0),
        ]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 2  # otherwise the sort-order assertion below passes vacuously
        assert [c.score for c in out] == sorted([c.score for c in out], reverse=True)

    def test_caps_to_max_clips_per_video(self, sample_transcript, test_settings):
        test_settings.min_clip_seconds = 0
        test_settings.max_clips_per_video = 1
        candidates = [
            _candidate(start_seconds=0.0, end_seconds=2.0, score=10.0),
            _candidate(start_seconds=5.0, end_seconds=6.2, score=90.0),
        ]
        out = _post_process(candidates, sample_transcript)
        assert len(out) == 1
        assert out[0].score == 90.0


@pytest.mark.unit
def test_analyze_calls_claude_and_post_processes(sample_transcript, mocker, test_settings):
    test_settings.min_clip_seconds = 0
    fake_result = AnalysisResult(clips=[_candidate(start_seconds=0.0, end_seconds=2.0)])
    mock_parse = mocker.patch("app.pipeline.analyze.claude_client.parse", return_value=fake_result)

    out = analyze(sample_transcript)

    assert len(out) == 1
    mock_parse.assert_called_once()
    _, kwargs = mock_parse.call_args
    assert kwargs["purpose"] == "analyze"
    assert "TRANSCRIPT" in kwargs["user"]
