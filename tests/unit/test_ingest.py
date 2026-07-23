"""Unit tests for app.pipeline.ingest — the ingest stage's own logic
(distinct from tests/unit/test_validation.py, which covers the shared
validation helpers ingest.py calls into). yt-dlp and ffprobe are mocked;
no network access or real media files are needed.

Mocking note: `ingest.py` imports `get_duration`/`validate_media_file` via
`from module import name`, which binds an independent reference in
`ingest`'s own namespace — patches must target `app.pipeline.ingest.<name>`,
not the original `app.core.validation.<name>` / `app.pipeline.ffmpeg_utils.<name>`
location, or the mock silently never takes effect (the classic
"patch where it's looked up, not where it's defined" mocking pitfall).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import IngestError, UnsupportedSourceError, ValidationError
from app.models import SourceType
from app.pipeline import ingest


@pytest.mark.unit
class TestIngestYoutube:
    def test_happy_path_returns_result(self, test_settings, mocker):
        # ingest.ingest() computes its own work dir internally
        # (settings.work_dir / video_id) — the fake "downloaded" file must
        # land exactly there for the post-download `source.mp4` lookup to
        # find it, since yt-dlp itself is mocked and writes nothing for real.
        work = test_settings.work_dir / "vid-1"
        work.mkdir(parents=True)
        (work / "source.mp4").write_bytes(b"fake video")

        fake_ydl = mocker.MagicMock()
        fake_ydl.__enter__.return_value.extract_info.return_value = {
            "title": "A Great Video",
            "duration": 120.5,
        }
        mocker.patch("yt_dlp.YoutubeDL", return_value=fake_ydl)
        mocker.patch("app.pipeline.ingest.validate_media_file", return_value={})

        result = ingest.ingest("vid-1", SourceType.youtube, "https://youtu.be/abc123")

        assert result.title == "A Great Video"
        assert result.duration == 120.5
        assert result.path == work / "source.mp4"

    def test_rejects_disallowed_host_before_calling_yt_dlp(self, mocker):
        spy = mocker.patch("yt_dlp.YoutubeDL")
        with pytest.raises(UnsupportedSourceError):
            ingest.ingest("vid-2", SourceType.youtube, "https://evil.example.com/x")
        spy.assert_not_called()

    def test_wraps_yt_dlp_download_error(self, mocker):
        import yt_dlp

        fake_ydl = mocker.MagicMock()
        fake_ydl.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "Video unavailable"
        )
        mocker.patch("yt_dlp.YoutubeDL", return_value=fake_ydl)

        with pytest.raises(IngestError, match="Video unavailable"):
            ingest.ingest("vid-3", SourceType.youtube, "https://youtu.be/gone")

    def test_raises_when_no_output_file_found(self, test_settings, mocker):
        fake_ydl = mocker.MagicMock()
        fake_ydl.__enter__.return_value.extract_info.return_value = {"title": "t", "duration": 5}
        mocker.patch("yt_dlp.YoutubeDL", return_value=fake_ydl)
        # No file written to the work dir at all.
        with pytest.raises(IngestError, match="no output file"):
            ingest.ingest("vid-4", SourceType.youtube, "https://youtu.be/x")

    def test_falls_back_to_alternate_extension(self, test_settings, mocker):
        work = test_settings.work_dir / "vid-5"
        work.mkdir(parents=True)
        (work / "source.webm").write_bytes(b"fake webm")

        fake_ydl = mocker.MagicMock()
        fake_ydl.__enter__.return_value.extract_info.return_value = {"title": "t", "duration": 5}
        mocker.patch("yt_dlp.YoutubeDL", return_value=fake_ydl)
        mocker.patch("app.pipeline.ingest.validate_media_file", return_value={})

        result = ingest.ingest("vid-5", SourceType.youtube, "https://youtu.be/x")
        assert result.path.suffix == ".webm"


@pytest.mark.unit
class TestIngestLocal:
    def test_copies_file_from_input_dir(self, test_settings, mocker):
        test_settings.input_dir.mkdir(parents=True, exist_ok=True)
        src = test_settings.input_dir / "clip.mp4"
        src.write_bytes(b"a" * 1000)

        # get_duration (called inside _ingest_local) and validate_media_file
        # (called by the public ingest() wrapper afterwards) both shell out
        # to a real ffprobe on a real file — mocked here since this fake
        # "video" is just raw bytes, and this test is about the copy/
        # path-resolution logic, not ffprobe integration (that's covered by
        # tests/integration/test_full_pipeline.py's real-ffmpeg variant).
        mocker.patch("app.pipeline.ingest.get_duration", return_value=42.0)
        mocker.patch("app.pipeline.ingest.validate_media_file", return_value={})

        result = ingest.ingest("vid-6", SourceType.upload, "clip.mp4")

        assert result.path.exists()
        assert result.path.read_bytes() == b"a" * 1000
        assert result.title == "clip"
        assert result.duration == 42.0

    def test_rejects_path_outside_input_dir(self, test_settings):
        with pytest.raises(ValidationError):
            ingest.ingest("vid-7", SourceType.upload, "../../etc/passwd")

    def test_rejects_missing_file(self, test_settings):
        with pytest.raises(ValidationError):
            ingest.ingest("vid-8", SourceType.local, "does-not-exist.mp4")
