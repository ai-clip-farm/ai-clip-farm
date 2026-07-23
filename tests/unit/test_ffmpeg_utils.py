"""Unit tests for app.pipeline.ffmpeg_utils — timeout handling, error
wrapping, and the hardware-encoder-with-libx264-fallback logic. All
`subprocess.run` calls are mocked; no real ffmpeg binary is needed.
"""

from __future__ import annotations

import subprocess

import pytest

from app.core.exceptions import FFmpegExecutionError, FFmpegTimeoutError
from app.pipeline import ffmpeg_utils


@pytest.mark.unit
class TestRun:
    def test_returns_stdout_on_success(self, mocker):
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
        )
        assert ffmpeg_utils.run(["ffmpeg", "-version"]) == "ok"

    def test_raises_execution_error_on_nonzero_exit(self, mocker):
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="invalid data found"
            ),
        )
        with pytest.raises(FFmpegExecutionError, match="invalid data found"):
            ffmpeg_utils.run(["ffmpeg", "-i", "bad.mp4"])

    def test_raises_timeout_error_on_timeout(self, mocker):
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=5),
        )
        with pytest.raises(FFmpegTimeoutError):
            ffmpeg_utils.run(["ffmpeg", "-i", "stuck.mp4"], timeout=5)

    def test_raises_execution_error_on_missing_binary(self, mocker):
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("no such file"))
        with pytest.raises(FFmpegExecutionError):
            ffmpeg_utils.run(["nonexistent-binary"])


@pytest.mark.unit
class TestProbeAndDuration:
    def test_probe_parses_json(self, mocker):
        mocker.patch(
            "app.pipeline.ffmpeg_utils.run",
            return_value='{"format": {"duration": "12.3"}, "streams": []}',
        )
        info = ffmpeg_utils.probe("video.mp4")
        assert info["format"]["duration"] == "12.3"

    def test_get_duration_returns_float(self, mocker):
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={"format": {"duration": "42.5"}},
        )
        assert ffmpeg_utils.get_duration("video.mp4") == 42.5

    def test_get_video_dimensions_finds_video_stream(self, mocker):
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={
                "streams": [
                    {"codec_type": "audio"},
                    {"codec_type": "video", "width": 1920, "height": 1080},
                ]
            },
        )
        assert ffmpeg_utils.get_video_dimensions("video.mp4") == (1920, 1080)

    def test_get_video_dimensions_raises_without_video_stream(self, mocker):
        mocker.patch(
            "app.pipeline.ffmpeg_utils.probe",
            return_value={"streams": [{"codec_type": "audio"}]},
        )
        with pytest.raises(FFmpegExecutionError):
            ffmpeg_utils.get_video_dimensions("audio.mp3")


@pytest.mark.unit
class TestHardwareEncoderFallback:
    def test_uses_libx264_when_hwaccel_none(self, test_settings):
        test_settings.ffmpeg_hwaccel = "none"
        args = ffmpeg_utils._video_encoder_args()
        assert args[:2] == ["-c:v", "libx264"]

    def test_uses_nvenc_when_available_and_requested(self, test_settings, mocker):
        test_settings.ffmpeg_hwaccel = "nvenc"
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=True)
        ffmpeg_utils.ffmpeg_hwaccel_encoder.cache_clear()
        args = ffmpeg_utils._video_encoder_args()
        assert args[:2] == ["-c:v", "h264_nvenc"]

    def test_falls_back_to_libx264_when_nvenc_unavailable(self, test_settings, mocker):
        test_settings.ffmpeg_hwaccel = "nvenc"
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=False)
        ffmpeg_utils.ffmpeg_hwaccel_encoder.cache_clear()
        args = ffmpeg_utils._video_encoder_args()
        assert args[:2] == ["-c:v", "libx264"]

    def test_run_encode_retries_with_libx264_on_hw_failure(self, mocker, test_settings):
        # Built from the real `_video_encoder_args()` shape (-c:v h264_nvenc
        # -preset p4 -cq N), not a hand-rolled shorter stand-in — a shorter
        # fake previously masked a real bug where the fallback spliced out
        # only "-c:v <encoder>" and left the rest of the encoder's own flags
        # (-preset p4, -cq N's value) behind as stray positional args that
        # ffmpeg then misread as an extra output filename.
        test_settings.ffmpeg_hwaccel = "nvenc"
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=True)
        ffmpeg_utils.ffmpeg_hwaccel_encoder.cache_clear()

        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if "h264_nvenc" in cmd:
                raise FFmpegExecutionError("nvenc not available on this device")
            return "ok"

        mocker.patch("app.pipeline.ffmpeg_utils.run", side_effect=fake_run)
        cmd = ["ffmpeg", "-i", "in.mp4", *ffmpeg_utils._video_encoder_args(), "out.mp4"]

        result = ffmpeg_utils._run_encode(cmd)

        assert result == "ok"
        assert len(calls) == 2
        fallback = calls[1]
        assert "libx264" in fallback
        assert "-cq" not in fallback
        assert "p4" not in fallback  # stale NVENC preset value must not leak into the fallback
        assert fallback[-1] == "out.mp4"  # trailing args must survive the splice

    def test_run_encode_does_not_retry_when_already_libx264(self, mocker):
        mocker.patch(
            "app.pipeline.ffmpeg_utils.run",
            side_effect=FFmpegExecutionError("disk full"),
        )
        cmd = ["ffmpeg", "-i", "in.mp4", "-c:v", "libx264", "out.mp4"]
        with pytest.raises(FFmpegExecutionError, match="disk full"):
            ffmpeg_utils._run_encode(cmd)
