"""Unit tests for app.core.validation — the SSRF/path-traversal/file-size
guards. These are the highest-value tests in the suite: every one of them
maps directly to a security bug found and fixed during the production audit.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import (
    CorruptedMediaError,
    FileTooLargeError,
    UnsupportedSourceError,
    ValidationError,
)
from app.core.validation import (
    resolve_local_source,
    sanitize_filename,
    validate_media_file,
    validate_upload_size,
    validate_youtube_url,
)


@pytest.mark.unit
class TestValidateYoutubeUrl:
    def test_accepts_standard_youtube_url(self):
        url = validate_youtube_url("https://www.youtube.com/watch?v=abc123")
        assert url == "https://www.youtube.com/watch?v=abc123"

    def test_accepts_short_youtu_be_url(self):
        validate_youtube_url("https://youtu.be/abc123")  # should not raise

    def test_strips_whitespace(self):
        assert validate_youtube_url("  https://youtu.be/abc123  ") == "https://youtu.be/abc123"

    def test_rejects_empty_string(self):
        with pytest.raises(ValidationError):
            validate_youtube_url("")

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValidationError):
            validate_youtube_url("ftp://youtube.com/video")

    def test_rejects_file_scheme(self):
        """The concrete SSRF/LFI vector this guard exists to close."""
        with pytest.raises(ValidationError):
            validate_youtube_url("file:///etc/passwd")

    def test_rejects_disallowed_host(self):
        with pytest.raises(UnsupportedSourceError):
            validate_youtube_url("https://evil.example.com/watch?v=abc")

    def test_rejects_internal_network_host(self):
        """SSRF guard: yt-dlp's generic extractor could otherwise be pointed
        at cloud metadata endpoints or internal services."""
        with pytest.raises(UnsupportedSourceError):
            validate_youtube_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_lookalike_host(self):
        """`youtube.com.evil.com` must not pass a naive substring check."""
        with pytest.raises(UnsupportedSourceError):
            validate_youtube_url("https://youtube.com.evil.com/watch?v=abc")

    def test_allows_subdomain_of_allowed_host(self):
        validate_youtube_url("https://m.youtube.com/watch?v=abc")  # should not raise


@pytest.mark.unit
class TestSanitizeFilename:
    def test_allows_normal_filename(self):
        assert sanitize_filename("my-video.mp4") == "my-video.mp4"

    def test_strips_directory_traversal(self):
        result = sanitize_filename("../../../etc/passwd.mp4")
        assert "/" not in result
        assert ".." not in result

    def test_strips_windows_style_traversal(self):
        result = sanitize_filename("..\\..\\windows\\system32\\evil.mp4")
        assert "\\" not in result
        assert ".." not in result

    def test_rejects_disallowed_extension(self):
        with pytest.raises(UnsupportedSourceError):
            sanitize_filename("script.sh")

    def test_rejects_empty_filename(self):
        with pytest.raises(ValidationError):
            sanitize_filename("")

    def test_slugifies_unsafe_characters(self):
        result = sanitize_filename("my video!! (final) <script>.mp4")
        assert result.endswith(".mp4")
        assert "<" not in result and ">" not in result

    def test_preserves_extension_case_insensitively(self):
        result = sanitize_filename("Clip.MP4")
        assert result.endswith(".mp4")


@pytest.mark.unit
class TestValidateUploadSize:
    def test_accepts_reasonable_size(self):
        validate_upload_size(10 * 1024 * 1024)  # should not raise

    def test_rejects_zero_size(self):
        with pytest.raises(ValidationError):
            validate_upload_size(0)

    def test_rejects_negative_size(self):
        with pytest.raises(ValidationError):
            validate_upload_size(-1)

    def test_rejects_oversized_file(self, test_settings):
        too_big = (test_settings.max_upload_size_mb + 1) * 1024 * 1024
        with pytest.raises(FileTooLargeError):
            validate_upload_size(too_big)


@pytest.mark.unit
class TestResolveLocalSource:
    def test_resolves_file_inside_input_dir(self, test_settings):
        test_settings.input_dir.mkdir(parents=True, exist_ok=True)
        f = test_settings.input_dir / "video.mp4"
        f.write_bytes(b"fake video content")
        resolved = resolve_local_source("video.mp4")
        assert resolved == f.resolve()

    def test_rejects_path_traversal_outside_input_dir(self, test_settings, tmp_path):
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"secret")
        with pytest.raises(ValidationError):
            resolve_local_source(f"../{outside.name}")

    def test_rejects_absolute_path_outside_sandbox(self, tmp_path):
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"secret")
        with pytest.raises(ValidationError):
            resolve_local_source(str(outside))

    def test_rejects_missing_file(self, test_settings):
        with pytest.raises(ValidationError):
            resolve_local_source("does-not-exist.mp4")


@pytest.mark.unit
class TestValidateMediaFile:
    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(CorruptedMediaError):
            validate_media_file(tmp_path / "nope.mp4")

    def test_rejects_empty_file(self, tmp_path):
        f = tmp_path / "empty.mp4"
        f.write_bytes(b"")
        with pytest.raises(CorruptedMediaError):
            validate_media_file(f)

    def test_rejects_when_ffprobe_fails(self, tmp_path, mocker):
        f = tmp_path / "garbage.mp4"
        f.write_bytes(b"not a real video file")
        from app.pipeline.ffmpeg_utils import FFmpegExecutionError

        mocker.patch("app.pipeline.ffmpeg_utils.probe", side_effect=FFmpegExecutionError("bad"))
        with pytest.raises(CorruptedMediaError):
            validate_media_file(f)

    def test_rejects_no_video_stream(self, tmp_path, mocker):
        f = tmp_path / "audio_only.mp4"
        f.write_bytes(b"fake")
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={"streams": [{"codec_type": "audio"}], "format": {"duration": "5.0"}},
        )
        with pytest.raises(CorruptedMediaError, match="no video stream"):
            validate_media_file(f)

    def test_rejects_zero_duration(self, tmp_path, mocker):
        f = tmp_path / "truncated.mp4"
        f.write_bytes(b"fake")
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={"streams": [{"codec_type": "video"}], "format": {"duration": "0.0"}},
        )
        mocker.patch("app.pipeline.ffmpeg_utils.get_duration", return_value=0.0)
        with pytest.raises(CorruptedMediaError, match="duration"):
            validate_media_file(f)

    def test_accepts_valid_media(self, tmp_path, mocker):
        f = tmp_path / "good.mp4"
        f.write_bytes(b"fake but non-empty")
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={"streams": [{"codec_type": "video"}], "format": {"duration": "12.5"}},
        )
        mocker.patch("app.pipeline.ffmpeg_utils.get_duration", return_value=12.5)
        info = validate_media_file(f)
        assert info["format"]["duration"] == "12.5"
