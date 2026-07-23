"""Unit tests for app.core.cleanup — workspace retention. Covers the disk-fill
bug this module fixes: work directories were previously never reclaimed."""

from __future__ import annotations

import time

import pytest

from app.core.cleanup import (
    cleanup_clip_workspace_on_failure,
    cleanup_video_workspace,
    purge_stale_work_dirs,
)


@pytest.mark.unit
class TestCleanupVideoWorkspace:
    def test_removes_video_workspace_when_enabled(self, test_settings):
        test_settings.cleanup_work_dir_on_success = True
        work = test_settings.work_dir / "vid-123"
        work.mkdir(parents=True)
        (work / "source.mp4").write_bytes(b"data")

        cleanup_video_workspace("vid-123")

        assert not work.exists()

    def test_does_nothing_when_disabled(self, test_settings):
        test_settings.cleanup_work_dir_on_success = False
        work = test_settings.work_dir / "vid-456"
        work.mkdir(parents=True)

        cleanup_video_workspace("vid-456")

        assert work.exists()

    def test_is_safe_to_call_on_missing_directory(self, test_settings):
        test_settings.cleanup_work_dir_on_success = True
        cleanup_video_workspace("does-not-exist")  # must not raise


@pytest.mark.unit
class TestCleanupClipWorkspaceOnFailure:
    def test_removes_when_keep_on_failure_disabled(self, test_settings):
        test_settings.keep_work_dir_on_failure = False
        clip_work = test_settings.work_dir / "vid-1" / "clips" / "clip-1"
        clip_work.mkdir(parents=True)

        cleanup_clip_workspace_on_failure("vid-1", "clip-1")

        assert not clip_work.exists()

    def test_keeps_when_flag_enabled(self, test_settings):
        test_settings.keep_work_dir_on_failure = True
        clip_work = test_settings.work_dir / "vid-2" / "clips" / "clip-2"
        clip_work.mkdir(parents=True)

        cleanup_clip_workspace_on_failure("vid-2", "clip-2")

        assert clip_work.exists()


@pytest.mark.unit
class TestPurgeStaleWorkDirs:
    def test_removes_entries_older_than_retention_window(self, test_settings):
        test_settings.work_dir_retention_hours = 1
        old_dir = test_settings.work_dir / "old-video"
        old_dir.mkdir(parents=True)
        old_time = time.time() - 3 * 3600
        import os

        os.utime(old_dir, (old_time, old_time))

        report = purge_stale_work_dirs()

        assert "old-video" in report["removed"]
        assert not old_dir.exists()

    def test_keeps_recent_entries(self, test_settings):
        test_settings.work_dir_retention_hours = 48
        recent_dir = test_settings.work_dir / "recent-video"
        recent_dir.mkdir(parents=True)

        report = purge_stale_work_dirs()

        assert "recent-video" not in report["removed"]
        assert recent_dir.exists()

    def test_handles_missing_work_dir_gracefully(self, test_settings, tmp_path):
        test_settings.work_dir = tmp_path / "nonexistent"
        report = purge_stale_work_dirs()
        assert report == {"removed": [], "errors": []}
