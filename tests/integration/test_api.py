"""Integration tests for the HTTP API — real FastAPI routing, dependency
injection, and request/response validation against the (SQLite) test
database. Celery's `.delay()` is mocked everywhere: these tests verify the
API's own contract, not that a broker is reachable (that's an infra concern,
smoke-tested separately — see docs/DEPLOYMENT.md's post-deploy checklist).
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ClipStatus, JobStatus, SourceType, Video


@pytest.fixture
def client(mocker):
    mocker.patch("app.api.routes.process_video.delay", return_value=mocker.Mock(id="task-1"))
    mocker.patch("app.api.routes.render_clip.delay", return_value=mocker.Mock(id="task-2"))
    mocker.patch("app.api.routes.failed_job_report.delay", return_value=mocker.Mock(id="task-3"))
    with TestClient(app) as c:
        yield c


@pytest.mark.integration
class TestHealthEndpoints:
    def test_health_is_always_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_ready_reports_database_status(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        assert resp.json()["checks"]["database"] is True

    def test_response_carries_request_id_header(self, client):
        resp = client.get("/health")
        assert "X-Request-ID" in resp.headers
        assert "X-Response-Time-ms" in resp.headers


@pytest.mark.integration
class TestCreateVideo:
    def test_creates_video_and_enqueues_task(self, client, mocker):
        resp = client.post(
            "/api/videos",
            json={"source_type": "youtube", "source_ref": "https://youtu.be/abc123", "title": "Test"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Test"
        assert body["status"] == "pending"

        import app.api.routes as routes
        routes.process_video.delay.assert_called_once()

    def test_rejects_disallowed_youtube_host(self, client):
        resp = client.post(
            "/api/videos",
            json={"source_type": "youtube", "source_ref": "https://evil.example.com/video"},
        )
        assert resp.status_code == 400

    def test_rejects_non_http_url(self, client):
        resp = client.post(
            "/api/videos",
            json={"source_type": "youtube", "source_ref": "file:///etc/passwd"},
        )
        assert resp.status_code == 400

    def test_missing_source_ref_is_422(self, client):
        resp = client.post("/api/videos", json={"source_type": "youtube"})
        assert resp.status_code == 422


@pytest.mark.integration
class TestUploadVideo:
    def test_uploads_and_enqueues(self, client, test_settings):
        test_settings.input_dir.mkdir(parents=True, exist_ok=True)
        file_content = b"fake mp4 bytes"
        resp = client.post(
            "/api/videos/upload",
            files={"file": ("clip.mp4", io.BytesIO(file_content), "video/mp4")},
            data={"title": "My Upload"},
        )
        assert resp.status_code == 201
        assert resp.json()["source_type"] == "upload"

    def test_rejects_disallowed_extension(self, client):
        resp = client.post(
            "/api/videos/upload",
            files={"file": ("script.sh", io.BytesIO(b"#!/bin/sh\necho hi"), "text/plain")},
        )
        assert resp.status_code == 400

    def test_rejects_path_traversal_filename(self, client, test_settings):
        test_settings.input_dir.mkdir(parents=True, exist_ok=True)
        resp = client.post(
            "/api/videos/upload",
            files={"file": ("../../evil.mp4", io.BytesIO(b"data"), "video/mp4")},
        )
        # Whatever survives client-side path stripping must still resolve
        # safely inside INPUT_DIR — either sanitized to a plain filename
        # (201) or, if empty after stripping, rejected (400). Either way it
        # must never escape INPUT_DIR.
        assert resp.status_code in (201, 400)
        if resp.status_code == 201:
            saved = list(test_settings.input_dir.iterdir())
            assert all(test_settings.input_dir in p.resolve().parents for p in saved)


@pytest.mark.integration
class TestListAndGetVideo:
    def test_list_videos_paginates(self, client, db_session):
        for i in range(3):
            db_session.add(
                Video(title=f"v{i}", source_type=SourceType.upload, source_ref=f"v{i}.mp4")
            )
        db_session.commit()

        resp = client.get("/api/videos?limit=2&offset=0")
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2

    def test_list_videos_filters_by_status(self, client, db_session):
        db_session.add(Video(title="ok", source_type=SourceType.upload, source_ref="a.mp4", status=JobStatus.completed))
        db_session.add(Video(title="bad", source_type=SourceType.upload, source_ref="b.mp4", status=JobStatus.failed))
        db_session.commit()

        resp = client.get("/api/videos?status=failed")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "bad"

    def test_get_video_404_when_missing(self, client):
        resp = client.get("/api/videos/does-not-exist")
        assert resp.status_code == 404

    def test_get_video_returns_clips_sorted_by_rank(self, client, db_session):
        video = Video(title="v", source_type=SourceType.upload, source_ref="v.mp4")
        db_session.add(video)
        db_session.commit()
        from app.models import Clip

        db_session.add(Clip(video_id=video.id, rank=2, start_seconds=0, end_seconds=1))
        db_session.add(Clip(video_id=video.id, rank=1, start_seconds=0, end_seconds=1))
        db_session.commit()

        resp = client.get(f"/api/videos/{video.id}")
        ranks = [c["rank"] for c in resp.json()["clips"]]
        assert ranks == [1, 2]


@pytest.mark.integration
class TestClipEndpoints:
    def test_download_404_when_no_output(self, client, db_session):
        from app.models import Clip

        video = Video(title="v", source_type=SourceType.upload, source_ref="v.mp4")
        db_session.add(video)
        db_session.commit()
        clip = Clip(video_id=video.id, rank=1, start_seconds=0, end_seconds=1)
        db_session.add(clip)
        db_session.commit()

        resp = client.get(f"/api/clips/{clip.id}/download")
        assert resp.status_code == 404

    def test_rerender_enqueues_task(self, client, db_session):
        from app.models import Clip

        video = Video(title="v", source_type=SourceType.upload, source_ref="v.mp4")
        db_session.add(video)
        db_session.commit()
        clip = Clip(video_id=video.id, rank=1, start_seconds=0, end_seconds=1, status=ClipStatus.failed)
        db_session.add(clip)
        db_session.commit()

        resp = client.post(f"/api/clips/{clip.id}/rerender")
        assert resp.status_code == 200

        import app.api.routes as routes
        routes.render_clip.delay.assert_called_once_with(clip.id)


@pytest.mark.integration
class TestAuthEnforcement:
    def test_requires_api_key_when_configured(self, client, test_settings):
        test_settings.api_key = "secret-123"
        try:
            resp = client.get("/api/videos")
            assert resp.status_code == 401
        finally:
            test_settings.api_key = ""

    def test_accepts_valid_api_key(self, client, test_settings):
        test_settings.api_key = "secret-123"
        try:
            resp = client.get("/api/videos", headers={"X-API-Key": "secret-123"})
            assert resp.status_code == 200
        finally:
            test_settings.api_key = ""
