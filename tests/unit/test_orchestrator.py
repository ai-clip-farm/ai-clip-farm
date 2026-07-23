"""Unit tests for app.pipeline.orchestrator — the DB state machine that
chains ingest -> transcribe -> analyze -> (per clip) cut -> reframe ->
subtitles -> metadata. Every external stage call is mocked; what's under
test is exclusively orchestrator.py's own logic: job bookkeeping, status
transitions, cleanup timing, and duplicate-run protection.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import DuplicateJobError, RenderError
from app.models import Clip, ClipStatus, Job, JobStatus, Video
from app.models.schema import SourceType
from app.pipeline import orchestrator


def _make_video(db_session, **overrides) -> Video:
    defaults = {"source_type": SourceType.youtube, "source_ref": "https://youtu.be/x"}
    defaults.update(overrides)
    video = Video(**defaults)
    db_session.add(video)
    db_session.commit()
    return video


def _make_clip(db_session, video: Video, **overrides) -> Clip:
    defaults = {
        "video_id": video.id,
        "rank": 1,
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "transcript_text": "hello",
        "status": ClipStatus.selected,
    }
    defaults.update(overrides)
    clip = Clip(**defaults)
    db_session.add(clip)
    db_session.commit()
    return clip


@pytest.mark.unit
class TestPrepareVideo:
    def test_happy_path_creates_jobs_and_clips(self, db_session, mocker, sample_transcript):
        video = _make_video(db_session)

        mocker.patch(
            "app.pipeline.orchestrator.ingest.ingest",
            return_value=SimpleNamespace(
                path=Path("/data/work/x/source.mp4"), title="My Video", duration=10.0
            ),
        )
        mocker.patch(
            "app.pipeline.orchestrator.transcribe.transcribe", return_value=sample_transcript
        )
        candidate = SimpleNamespace(
            start_seconds=0.0,
            end_seconds=2.0,
            score=90.0,
            reason="funny",
            categories=["funny"],
            transcript_text="hello",
        )
        mocker.patch("app.pipeline.orchestrator.analyze.analyze", return_value=[candidate])

        clip_ids = orchestrator.prepare_video(db_session, video.id)

        assert len(clip_ids) == 1
        db_session.refresh(video)
        assert video.status == JobStatus.running  # set at start; render/finalize flips it later
        assert video.title == "My Video"
        assert video.duration_seconds == 10.0

        jobs = db_session.query(Job).filter_by(video_id=video.id).all()
        stages = {j.stage: j.status for j in jobs}
        assert stages == {
            "ingest": JobStatus.completed,
            "transcribe": JobStatus.completed,
            "analyze": JobStatus.completed,
        }

    def test_refuses_concurrent_run_on_same_video(self, db_session):
        video = _make_video(db_session, status=JobStatus.running)
        with pytest.raises(DuplicateJobError):
            orchestrator.prepare_video(db_session, video.id)

    def test_ingest_failure_marks_video_failed(self, db_session, mocker):
        video = _make_video(db_session)
        from app.core.exceptions import IngestError

        mocker.patch(
            "app.pipeline.orchestrator.ingest.ingest",
            side_effect=IngestError("network blip"),
        )

        with pytest.raises(IngestError):
            orchestrator.prepare_video(db_session, video.id)

        db_session.refresh(video)
        assert video.status == JobStatus.failed
        assert "network blip" in video.error

    def test_unknown_video_raises_value_error(self, db_session):
        with pytest.raises(ValueError):
            orchestrator.prepare_video(db_session, "does-not-exist")


@pytest.mark.unit
class TestRenderClip:
    def _mock_stages(self, mocker, work_dir: Path):
        mocker.patch("app.pipeline.orchestrator.cut.cut", return_value=work_dir / "cut.mp4")
        mocker.patch(
            "app.pipeline.orchestrator.reframe.reframe", return_value=work_dir / "framed.mp4"
        )
        mocker.patch(
            "app.pipeline.orchestrator.subtitles.build_ass", return_value=work_dir / "subs.ass"
        )
        mocker.patch("app.pipeline.orchestrator.subtitles.burn")
        mocker.patch("app.pipeline.orchestrator.make_thumbnail")
        meta = SimpleNamespace(
            title="Great Clip", hook="Wow!", description="desc", hashtags=["x", "y"]
        )
        mocker.patch("app.pipeline.orchestrator.metadata.generate", return_value=meta)
        return meta

    def test_happy_path_packages_output_and_cleans_workspace(
        self, db_session, mocker, sample_transcript, test_settings, tmp_path
    ):
        video = _make_video(db_session, title="Demo", transcript=sample_transcript)
        video.source_path = "/data/work/x/source.mp4"
        db_session.commit()
        clip = _make_clip(db_session, video)

        work = test_settings.work_dir / video.id / "clips" / clip.id
        self._mock_stages(mocker, work)

        result_path = orchestrator.render_clip(db_session, clip.id)

        db_session.refresh(clip)
        assert clip.status == ClipStatus.completed
        assert clip.gen_title == "Great Clip"
        assert clip.gen_hashtags == ["x", "y"]
        assert clip.render_started_at is not None
        assert clip.render_finished_at is not None
        assert Path(result_path).name == "clip.mp4"
        assert not work.exists()  # per-clip workspace reclaimed on success

    def test_render_failure_marks_clip_failed_and_records_error(
        self, db_session, mocker, sample_transcript
    ):
        video = _make_video(db_session, title="Demo", transcript=sample_transcript)
        video.source_path = "/data/work/x/source.mp4"
        db_session.commit()
        clip = _make_clip(db_session, video)

        mocker.patch(
            "app.pipeline.orchestrator.cut.cut", side_effect=RenderError("ffmpeg exploded")
        )

        with pytest.raises(RenderError):
            orchestrator.render_clip(db_session, clip.id)

        db_session.refresh(clip)
        assert clip.status == ClipStatus.failed
        assert "ffmpeg exploded" in clip.error

    def test_keeps_clip_workspace_on_failure_when_configured(
        self, db_session, mocker, sample_transcript, test_settings
    ):
        test_settings.keep_work_dir_on_failure = True
        video = _make_video(db_session, title="Demo", transcript=sample_transcript)
        video.source_path = "/data/work/x/source.mp4"
        db_session.commit()
        clip = _make_clip(db_session, video)

        work = test_settings.work_dir / video.id / "clips" / clip.id
        work.mkdir(parents=True)
        mocker.patch("app.pipeline.orchestrator.cut.cut", side_effect=RenderError("boom"))

        with pytest.raises(RenderError):
            orchestrator.render_clip(db_session, clip.id)

        assert work.exists()

    def test_missing_clip_raises_value_error(self, db_session):
        with pytest.raises(ValueError):
            orchestrator.render_clip(db_session, "does-not-exist")

    def test_missing_source_path_raises_value_error(self, db_session):
        video = _make_video(db_session)  # source_path never set
        clip = _make_clip(db_session, video)
        with pytest.raises(ValueError):
            orchestrator.render_clip(db_session, clip.id)


@pytest.mark.unit
class TestFinalizeVideo:
    def test_marks_completed_with_no_failures(self, db_session):
        video = _make_video(db_session, status=JobStatus.running)
        _make_clip(db_session, video, status=ClipStatus.completed)

        orchestrator.finalize_video(db_session, video.id)

        db_session.refresh(video)
        assert video.status == JobStatus.completed
        assert video.error is None

    def test_records_partial_failure_message(self, db_session):
        video = _make_video(db_session, status=JobStatus.running)
        _make_clip(db_session, video, rank=1, status=ClipStatus.completed)
        _make_clip(db_session, video, rank=2, status=ClipStatus.failed)

        orchestrator.finalize_video(db_session, video.id)

        db_session.refresh(video)
        assert video.status == JobStatus.completed
        assert "1/2" in video.error

    def test_records_total_failure_message(self, db_session):
        video = _make_video(db_session, status=JobStatus.running)
        _make_clip(db_session, video, status=ClipStatus.failed)

        orchestrator.finalize_video(db_session, video.id)

        db_session.refresh(video)
        assert "All 1 clips failed" in video.error

    def test_missing_video_is_a_no_op(self, db_session):
        orchestrator.finalize_video(db_session, "does-not-exist")  # must not raise
