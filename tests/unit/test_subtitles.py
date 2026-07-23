"""Unit tests for app.pipeline.subtitles — ASS generation, special-character
escaping (the fix for transcribed words containing `{`/`}`/`\` corrupting
override tags), and the empty-clip guard. No ffmpeg process is invoked here;
`burn()` (which shells out) is covered by test_ffmpeg_utils.py instead.
"""
from __future__ import annotations

import pytest

from app.core.exceptions import RenderError
from app.pipeline.subtitles import (
    STYLE_PRESETS,
    _clip_words,
    _escape_ass_text,
    _fmt_time,
    _group_lines,
    build_ass,
)


@pytest.mark.unit
class TestFmtTime:
    def test_zero(self):
        assert _fmt_time(0.0) == "0:00:00.00"

    def test_sub_second(self):
        assert _fmt_time(1.5) == "0:00:01.50"

    def test_minutes(self):
        assert _fmt_time(65.25) == "0:01:05.25"

    def test_never_negative(self):
        assert _fmt_time(-1.0) == "0:00:00.00"


@pytest.mark.unit
class TestEscapeAssText:
    def test_escapes_braces(self):
        assert _escape_ass_text("{not a tag}") == "\\{not a tag\\}"

    def test_escapes_backslash(self):
        assert _escape_ass_text("C:\\path") == "C:\\\\path"

    def test_replaces_newlines_with_space(self):
        assert "\n" not in _escape_ass_text("line one\nline two")

    def test_leaves_normal_text_untouched(self):
        assert _escape_ass_text("hello world") == "hello world"


@pytest.mark.unit
class TestClipWords:
    def test_extracts_words_in_range_rebased_to_zero(self, sample_transcript):
        words = _clip_words(sample_transcript, 0.0, 2.0)
        assert words[0]["word"] == "Hello"
        assert words[0]["start"] == 0.0

    def test_excludes_words_outside_range(self, sample_transcript):
        words = _clip_words(sample_transcript, 0.0, 2.0)
        assert all(w["word"] != "Second" for w in words)

    def test_second_segment_rebased_correctly(self, sample_transcript):
        words = _clip_words(sample_transcript, 5.0, 6.2)
        assert words[0]["word"] == "Second"
        assert words[0]["start"] == 0.0  # 5.0 - 5.0

    def test_empty_when_range_has_no_words(self, sample_transcript):
        assert _clip_words(sample_transcript, 2.5, 4.9) == []


@pytest.mark.unit
def test_group_lines_chunks_by_words_per_line():
    words = [{"word": str(i)} for i in range(10)]
    groups = _group_lines(words, 4)
    assert [len(g) for g in groups] == [4, 4, 2]


@pytest.mark.unit
class TestBuildAss:
    def test_builds_file_with_expected_header_fields(self, sample_transcript, tmp_path, test_settings):
        dst = tmp_path / "subs.ass"
        build_ass(sample_transcript, 0.0, 2.0, dst)
        content = dst.read_text(encoding="utf-8")
        assert "[Script Info]" in content
        assert f"PlayResX: {test_settings.target_width}" in content
        assert "[Events]" in content
        assert "Dialogue:" in content

    def test_karaoke_timing_tags_present(self, sample_transcript, tmp_path):
        dst = tmp_path / "subs.ass"
        build_ass(sample_transcript, 0.0, 2.0, dst)
        content = dst.read_text(encoding="utf-8")
        assert "\\kf" in content

    def test_raises_render_error_when_no_words_in_range(self, sample_transcript, tmp_path):
        with pytest.raises(RenderError):
            build_ass(sample_transcript, 2.5, 4.9, tmp_path / "subs.ass")

    def test_special_characters_in_words_are_escaped(self, tmp_path):
        transcript = {
            "segments": [
                {
                    "start": 0.0, "end": 1.0, "text": "{evil} tag",
                    "words": [
                        {"start": 0.0, "end": 0.5, "word": "{evil}"},
                        {"start": 0.5, "end": 1.0, "word": "tag"},
                    ],
                }
            ]
        }
        dst = tmp_path / "subs.ass"
        build_ass(transcript, 0.0, 1.0, dst)
        content = dst.read_text(encoding="utf-8")
        # The literal, un-escaped word must not appear as raw {evil} outside
        # of legitimate override tags — it should be escaped to \{evil\}.
        assert "\\{evil\\}" in content

    @pytest.mark.parametrize("style_name", list(STYLE_PRESETS.keys()))
    def test_every_style_preset_produces_valid_output(self, style_name, sample_transcript, tmp_path, test_settings):
        test_settings.subtitle_style = style_name
        dst = tmp_path / f"{style_name}.ass"
        build_ass(sample_transcript, 0.0, 2.0, dst)
        assert dst.exists()
        assert STYLE_PRESETS[style_name]["font"] in dst.read_text(encoding="utf-8")
