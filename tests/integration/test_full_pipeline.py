"""End-to-end pipeline tests — the "every stage must be tested, together"
requirement.

Two variants:

  TestFullPipelineMocked — always runs, no external binaries required. Wires
  `orchestrator.prepare_video` -> `render_clip` (once per created clip) ->
  `finalize_video` exactly as the Celery task chain in workers/tasks.py does,
  with every external call (yt-dlp, Whisper, Claude, ffmpeg, OpenCV) mocked.
  This is the fast, deterministic check that the *sequence* is wired
  correctly and DB state ends up right.

  TestFullPipelineReal — exercises real ffmpeg + OpenCV against a tiny
  synthetic video generated on the fly (`ffmpeg -f lavfi testsrc`), with only
  Claude and the transcript mocked (no network calls, no GPU/model
  download). This catches real integration bugs — wrong ffmpeg flags, codec
  issues, ASS path escaping — that pure-mock tests structurally cannot.
  Skipped automatically if `ffmpeg`/`ffprobe`/OpenCV aren't available, so it
  never produces a false failure on a minimal machine, but runs for real in
  CI (see .github/workflows/ci.yml, which installs ffmpeg before pytest).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ClipStatus, JobStatus, SourceType, Video
from app.pipeline import orchestrator

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
try:
    import cv2  # noqa: F401

    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False


@pytest.mark.integration
class TestFullPipelineMocked:
    def test_prepare_render_finalize_happy_path(
        self, db_session, mocker, sample_transcript, test_settings
    ):
        video = Video(
            title="", source_type=SourceType.youtube, source_ref="https://youtu.be/abc123"
        )
        db_session.add(video)
        db_session.commit()

        mocker.patch(
            "app.pipeline.orchestrator.ingest.ingest",
            return_value=SimpleNamespace(
                path=Path("/data/work/x/source.mp4"), title="Great Podcast", duration=10.0
            ),
        )
        mocker.patch(
            "app.pipeline.orchestrator.transcribe.transcribe", return_value=sample_transcript
        )

        candidates = [
            SimpleNamespace(
                start_seconds=0.0,
                end_seconds=2.0,
                score=95.0,
                reason="hook",
                categories=["hook"],
                transcript_text="Hello world, this is a test.",
            ),
            SimpleNamespace(
                start_seconds=5.0,
                end_seconds=6.2,
                score=80.0,
                reason="funny",
                categories=["funny"],
                transcript_text="Second segment here.",
            ),
        ]
        mocker.patch("app.pipeline.orchestrator.analyze.analyze", return_value=candidates)

        # --- Stage 1: prepare (ingest -> transcribe -> analyze) ---
        clip_ids = orchestrator.prepare_video(db_session, video.id)
        assert len(clip_ids) == 2

        db_session.refresh(video)
        assert video.title == "Great Podcast"
        assert video.transcript == sample_transcript

        # --- Stage 2: render each clip (cut -> reframe -> subtitles -> metadata) ---
        mocker.patch("app.pipeline.orchestrator.cut.cut", side_effect=lambda src, dst, s, e: dst)
        mocker.patch(
            "app.pipeline.orchestrator.reframe.reframe", side_effect=lambda src, dst, w: dst
        )
        mocker.patch(
            "app.pipeline.orchestrator.subtitles.build_ass", side_effect=lambda t, s, e, dst: dst
        )
        mocker.patch(
            "app.pipeline.orchestrator.subtitles.burn",
            side_effect=lambda framed, ass, dst: dst.write_bytes(b"fake"),
        )
        mocker.patch(
            "app.pipeline.orchestrator.make_thumbnail",
            side_effect=lambda src, dst, at=0.5: dst.write_bytes(b"jpg"),
        )
        mocker.patch(
            "app.pipeline.orchestrator.metadata.generate",
            return_value=SimpleNamespace(
                title="Hooked in 2 Seconds",
                hook="Wait for it...",
                description="A great moment.",
                hashtags=["viral", "podcast"],
            ),
        )

        for clip_id in clip_ids:
            orchestrator.render_clip(db_session, clip_id)

        clips = (
            db_session.query(orchestrator.Clip).filter(orchestrator.Clip.video_id == video.id).all()
        )
        assert len(clips) == 2
        assert all(c.status == ClipStatus.completed for c in clips)
        assert all(Path(c.output_path).exists() for c in clips)
        assert all((Path(c.output_path).parent / "metadata.json").exists() for c in clips)

        # --- Stage 3: finalize ---
        orchestrator.finalize_video(db_session, video.id)
        db_session.refresh(video)
        assert video.status == JobStatus.completed
        assert video.error is None
        assert not (test_settings.work_dir / video.id).exists()  # workspace reclaimed

    def test_all_clips_failing_surfaces_in_finalize(self, db_session, mocker, sample_transcript):
        video = Video(title="v", source_type=SourceType.upload, source_ref="v.mp4")
        db_session.add(video)
        db_session.commit()

        mocker.patch(
            "app.pipeline.orchestrator.ingest.ingest",
            return_value=SimpleNamespace(path=Path("/x/source.mp4"), title="v", duration=5.0),
        )
        mocker.patch(
            "app.pipeline.orchestrator.transcribe.transcribe", return_value=sample_transcript
        )
        mocker.patch(
            "app.pipeline.orchestrator.analyze.analyze",
            return_value=[
                SimpleNamespace(
                    start_seconds=0.0,
                    end_seconds=2.0,
                    score=50.0,
                    reason="r",
                    categories=["viral"],
                    transcript_text="hi",
                )
            ],
        )
        clip_ids = orchestrator.prepare_video(db_session, video.id)

        from app.core.exceptions import RenderError

        mocker.patch("app.pipeline.orchestrator.cut.cut", side_effect=RenderError("corrupt source"))
        for clip_id in clip_ids:
            with pytest.raises(RenderError):
                orchestrator.render_clip(db_session, clip_id)

        orchestrator.finalize_video(db_session, video.id)
        db_session.refresh(video)
        assert video.status == JobStatus.completed  # completed = "attempted", not "succeeded"
        assert "failed to render" in video.error


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not available on this machine")
@pytest.mark.skipif(not _HAS_CV2, reason="opencv not installed")
class TestFullPipelineReal:
    """Runs the render half of the pipeline against a real, tiny synthetic
    video — no network, no GPU, no Whisper model download. This is the test
    that would have caught, e.g., the reframe.py resource-leak bug or a
    wrong ffmpeg flag, which pure-mock tests cannot."""

    @pytest.fixture
    def synthetic_video(self, tmp_path) -> Path:
        """A 4-second, 480x270 test-pattern video with a sine-wave audio
        track — generated entirely by ffmpeg's `lavfi` virtual devices, so
        no binary test asset needs to ship with the repo."""
        out = tmp_path / "synthetic.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=4:size=480x270:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=4",
                "-shortest",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            check=True,
            timeout=60,
        )
        return out

    def test_cut_reframe_subtitle_burn_produces_valid_output(
        self, synthetic_video, tmp_path, test_settings, db_session, mocker
    ):
        test_settings.tracking_backend = "center"  # no MediaPipe model needed
        test_settings.ffmpeg_hwaccel = "none"  # deterministic on CI runners without a GPU

        video = Video(
            title="Synthetic",
            source_type=SourceType.upload,
            source_ref="synthetic.mp4",
            source_path=str(synthetic_video),
            duration_seconds=4.0,
        )
        transcript = {
            "language": "en",
            "duration": 4.0,
            "segments": [
                {
                    "start": 0.0,
                    "end": 3.0,
                    "text": "Hello synthetic world.",
                    "words": [
                        {"start": 0.0, "end": 1.0, "word": "Hello"},
                        {"start": 1.0, "end": 2.0, "word": "synthetic"},
                        {"start": 2.0, "end": 3.0, "word": "world."},
                    ],
                }
            ],
        }
        video.transcript = transcript
        db_session.add(video)
        db_session.commit()

        from app.models import Clip

        clip = Clip(
            video_id=video.id,
            rank=1,
            start_seconds=0.0,
            end_seconds=3.0,
            transcript_text="Hello synthetic world.",
            status=ClipStatus.selected,
        )
        db_session.add(clip)
        db_session.commit()

        mocker.patch(
            "app.pipeline.orchestrator.metadata.generate",
            return_value=SimpleNamespace(
                title="Real Render Test",
                hook="Real ffmpeg!",
                description="Produced by real ffmpeg + OpenCV.",
                hashtags=["test"],
            ),
        )

        result_path = orchestrator.render_clip(db_session, clip.id)

        out = Path(result_path)
        assert out.exists()
        assert out.stat().st_size > 0

        # Confirm the output is genuinely a valid, playable 9:16 video —
        # not just a file that happens to exist.
        from app.pipeline.ffmpeg_utils import get_video_dimensions

        w, h = get_video_dimensions(out)
        assert (w, h) == (test_settings.target_width, test_settings.target_height)

        db_session.refresh(clip)
        assert clip.status == ClipStatus.completed
        assert Path(clip.thumbnail_path).exists()
