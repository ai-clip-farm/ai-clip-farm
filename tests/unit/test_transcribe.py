"""Unit tests for app.pipeline.transcribe — model caching (the fix for
reloading a 1.5GB model on every single video), device-resolution
integration, and error wrapping. `faster_whisper.WhisperModel` is mocked;
no real model download or audio decoding happens.
"""
from __future__ import annotations

import pytest

from app.core.exceptions import CorruptedMediaError, TranscriptionError
from app.pipeline import transcribe


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """The whole point of the module under test is a process-level cache —
    tests must not leak a mock model instance into unrelated tests."""
    transcribe._model_cache.clear()
    yield
    transcribe._model_cache.clear()


def _fake_segment(start, end, text, words):
    from types import SimpleNamespace

    return SimpleNamespace(
        start=start, end=end, text=text,
        words=[SimpleNamespace(start=w[0], end=w[1], word=w[2]) for w in words],
    )


@pytest.mark.unit
class TestModelCaching:
    def test_loads_model_only_once_across_multiple_calls(self, test_settings, mocker, tmp_path):
        test_settings.whisper_cache_models = True
        mock_model_cls = mocker.patch("faster_whisper.WhisperModel")
        mock_instance = mock_model_cls.return_value
        mock_instance.transcribe.return_value = (
            iter([_fake_segment(0.0, 1.0, "hi", [(0.0, 1.0, "hi")])]),
            mocker.Mock(language="en"),
        )
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=tmp_path / "a.wav")
        (tmp_path / "a.wav").write_bytes(b"fake audio")
        mocker.patch("app.pipeline.transcribe.get_duration", return_value=1.0)
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)

        transcribe.transcribe(tmp_path / "video.mp4", tmp_path)
        transcribe.transcribe(tmp_path / "video2.mp4", tmp_path)

        assert mock_model_cls.call_count == 1

    def test_reloads_when_caching_disabled(self, test_settings, mocker, tmp_path):
        test_settings.whisper_cache_models = False
        mock_model_cls = mocker.patch("faster_whisper.WhisperModel")
        mock_model_cls.return_value.transcribe.return_value = (
            iter([_fake_segment(0.0, 1.0, "hi", [(0.0, 1.0, "hi")])]),
            mocker.Mock(language="en"),
        )
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=tmp_path / "a.wav")
        (tmp_path / "a.wav").write_bytes(b"fake audio")
        mocker.patch("app.pipeline.transcribe.get_duration", return_value=1.0)
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)

        transcribe.transcribe(tmp_path / "video.mp4", tmp_path)
        transcribe.transcribe(tmp_path / "video2.mp4", tmp_path)

        assert mock_model_cls.call_count == 2

    def test_wraps_model_load_failure(self, test_settings, mocker):
        test_settings.whisper_cache_models = True
        mocker.patch("faster_whisper.WhisperModel", side_effect=RuntimeError("out of memory"))

        with pytest.raises(TranscriptionError, match="Failed to load Whisper model"):
            transcribe._get_model("large-v3", "cpu", "int8")


@pytest.mark.unit
class TestTranscribeFlow:
    def test_rejects_empty_extracted_audio(self, mocker, tmp_path):
        empty_audio = tmp_path / "empty.wav"
        empty_audio.write_bytes(b"")
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=empty_audio)

        with pytest.raises(CorruptedMediaError):
            transcribe.transcribe(tmp_path / "video.mp4", tmp_path)

    def test_wraps_audio_extraction_failure(self, mocker, tmp_path):
        mocker.patch(
            "app.pipeline.transcribe.extract_audio", side_effect=RuntimeError("ffmpeg exploded")
        )
        with pytest.raises(TranscriptionError, match="Failed to extract audio"):
            transcribe.transcribe(tmp_path / "video.mp4", tmp_path)

    def test_unknown_backend_raises(self, test_settings, mocker, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake audio")
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=audio)
        # Pydantic's Literal validation only runs at construction time, not
        # on a plain attribute set against an already-built instance — this
        # simulates a value that shouldn't be reachable via .env parsing, to
        # exercise the defensive else-branch in transcribe().
        test_settings.whisper_backend = "some_other_backend"

        with pytest.raises(TranscriptionError, match="Unknown WHISPER_BACKEND"):
            transcribe.transcribe(tmp_path / "video.mp4", tmp_path)

    def test_raises_when_zero_segments_produced(self, mocker, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake audio")
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=audio)
        mocker.patch("app.pipeline.transcribe.get_duration", return_value=1.0)
        mock_model_cls = mocker.patch("faster_whisper.WhisperModel")
        mock_model_cls.return_value.transcribe.return_value = (iter([]), mocker.Mock(language="en"))
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)

        with pytest.raises(TranscriptionError, match="zero segments"):
            transcribe.transcribe(tmp_path / "video.mp4", tmp_path)

    def test_progress_callback_failure_does_not_abort_transcription(self, mocker, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake audio")
        mocker.patch("app.pipeline.transcribe.extract_audio", return_value=audio)
        mocker.patch("app.pipeline.transcribe.get_duration", return_value=1.0)
        mock_model_cls = mocker.patch("faster_whisper.WhisperModel")
        mock_model_cls.return_value.transcribe.return_value = (
            iter([_fake_segment(0.0, 1.0, "hi", [(0.0, 1.0, "hi")])]),
            mocker.Mock(language="en"),
        )
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)

        def flaky_progress(p, m):
            raise RuntimeError("DB write failed transiently")

        result = transcribe.transcribe(tmp_path / "video.mp4", tmp_path, on_progress=flaky_progress)
        assert len(result["segments"]) == 1

    def test_full_text_joins_segment_text(self):
        transcript = {"segments": [{"text": "Hello"}, {"text": "world"}]}
        assert transcribe.full_text(transcript) == "Hello world"
