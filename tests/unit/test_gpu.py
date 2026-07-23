"""Unit tests for app.core.gpu — device auto-detection and fallback.

Every function here is `lru_cache`d for the process lifetime (detection is
a subprocess call, not free) — tests must clear the cache before/after
patching the underlying probe, or a result cached by an earlier test leaks
into a later one.
"""

from __future__ import annotations

import pytest

from app.core import gpu


@pytest.fixture(autouse=True)
def _clear_gpu_caches():
    gpu.nvidia_smi_available.cache_clear()
    gpu.ctranslate2_cuda_available.cache_clear()
    gpu.resolve_whisper_device.cache_clear()
    gpu.ffmpeg_hwaccel_encoder.cache_clear()
    yield
    gpu.nvidia_smi_available.cache_clear()
    gpu.ctranslate2_cuda_available.cache_clear()
    gpu.resolve_whisper_device.cache_clear()
    gpu.ffmpeg_hwaccel_encoder.cache_clear()


@pytest.mark.unit
class TestResolveWhisperDevice:
    def test_cpu_stays_cpu_regardless_of_gpu_presence(self, mocker):
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=True)
        assert gpu.resolve_whisper_device("cpu") == "cpu"

    def test_auto_uses_cuda_when_available(self, mocker):
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=True)
        assert gpu.resolve_whisper_device("auto") == "cuda"

    def test_auto_falls_back_to_cpu_when_unavailable(self, mocker):
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)
        assert gpu.resolve_whisper_device("auto") == "cpu"

    def test_explicit_cuda_falls_back_with_warning_when_unavailable(self, mocker):
        mocker.patch("app.core.gpu.ctranslate2_cuda_available", return_value=False)
        # Must not raise — a worker inheriting a GPU-oriented config on a
        # CPU-only host should degrade gracefully, not crash.
        assert gpu.resolve_whisper_device("cuda") == "cpu"


@pytest.mark.unit
class TestFfmpegHwaccelEncoder:
    def test_none_disables_hardware_encoding(self):
        assert gpu.ffmpeg_hwaccel_encoder("none") is None

    def test_nvenc_requires_nvidia_smi(self, mocker):
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=False)
        assert gpu.ffmpeg_hwaccel_encoder("nvenc") is None

    def test_nvenc_selected_when_gpu_present(self, mocker):
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=True)
        assert gpu.ffmpeg_hwaccel_encoder("nvenc") == "h264_nvenc"

    def test_auto_prefers_nvenc_when_available(self, mocker):
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=True)
        assert gpu.ffmpeg_hwaccel_encoder("auto") == "h264_nvenc"

    def test_auto_falls_back_to_software_without_gpu(self, mocker):
        mocker.patch("app.core.gpu.nvidia_smi_available", return_value=False)
        assert gpu.ffmpeg_hwaccel_encoder("auto") is None

    def test_vaapi_returned_without_a_probe(self):
        assert gpu.ffmpeg_hwaccel_encoder("vaapi") == "h264_vaapi"


@pytest.mark.unit
class TestNvidiaSmiAvailable:
    def test_false_when_binary_missing(self, mocker):
        mocker.patch("shutil.which", return_value=None)
        assert gpu.nvidia_smi_available() is False

    def test_false_on_timeout(self, mocker):
        import subprocess

        mocker.patch("shutil.which", return_value="/usr/bin/nvidia-smi")
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["nvidia-smi"], timeout=5),
        )
        assert gpu.nvidia_smi_available() is False

    def test_true_when_gpu_listed(self, mocker):
        import subprocess

        mocker.patch("shutil.which", return_value="/usr/bin/nvidia-smi")
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="GPU 0: Tesla T4", stderr=""
            ),
        )
        assert gpu.nvidia_smi_available() is True
