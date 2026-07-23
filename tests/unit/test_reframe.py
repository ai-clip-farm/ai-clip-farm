"""Unit tests for app.pipeline.reframe — trajectory smoothing (pure math,
no video I/O) and the frame-detect-striding logic (the main reframe.py
performance lever) against a duck-typed fake capture, plus the guard clauses
that fixed the resource-leak/zero-frame bugs found in the production audit.

Requires `opencv-python-headless`/`numpy` importable (both top-level imports
in reframe.py) — same constraint as the rest of this suite; installed via
requirements.txt in CI.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import RenderError
from app.pipeline import reframe


class _FakeCapture:
    """Duck-typed stand-in for cv2.VideoCapture — `_track_strided` only
    calls `.read()` in a loop, so a real VideoCapture (and therefore a real
    video file) isn't needed to test the striding logic in isolation."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._idx = 0

    def read(self):
        if self._idx >= len(self._frames):
            return False, None
        frame = self._frames[self._idx]
        self._idx += 1
        return True, frame


@pytest.mark.unit
class TestSmooth:
    def test_empty_input_returns_empty_output(self):
        assert reframe._smooth([], default=100.0) == []

    def test_fills_leading_none_with_default(self):
        out = reframe._smooth([None, None, 50.0], default=100.0)
        assert out[0] == pytest.approx(100.0, abs=1.0)

    def test_holds_last_known_value_through_gaps(self):
        out = reframe._smooth([200.0, None, None, None], default=0.0, alpha=1.0)
        # alpha=1.0 makes the EMA track instantly, so a held-last-known
        # value of 200.0 should persist through the None gap exactly.
        assert out[1] == 200.0
        assert out[2] == 200.0

    def test_converges_toward_a_constant_signal(self):
        out = reframe._smooth([500.0] * 20, default=0.0, alpha=0.12)
        assert out[-1] == pytest.approx(500.0, rel=0.05)

    def test_output_length_matches_input(self):
        centers = [100.0, None, 200.0, None, None]
        assert len(reframe._smooth(centers, default=50.0)) == len(centers)


@pytest.mark.unit
class TestTrackStrided:
    def test_detects_every_stride_frames_and_holds_between(self, test_settings):
        test_settings.face_detect_stride = 3
        cap = _FakeCapture(["f0", "f1", "f2", "f3", "f4", "f5"])
        calls = []

        def fake_detect(frame):
            calls.append(frame)
            return {"f0": 10.0, "f3": 20.0}.get(frame)

        centers = reframe._track_strided(cap, fake_detect)

        # Detection only invoked on frames 0 and 3 (stride=3), not all 6.
        assert calls == ["f0", "f3"]
        assert centers == [10.0, 10.0, 10.0, 20.0, 20.0, 20.0]

    def test_stride_one_detects_every_frame(self, test_settings):
        test_settings.face_detect_stride = 1
        cap = _FakeCapture(["f0", "f1", "f2"])
        calls = []

        def fake_detect(frame):
            calls.append(frame)
            return 1.0

        reframe._track_strided(cap, fake_detect)
        assert len(calls) == 3

    def test_empty_capture_returns_empty_list(self):
        cap = _FakeCapture([])
        assert reframe._track_strided(cap, lambda f: 1.0) == []


@pytest.mark.unit
class TestReframeGuards:
    def test_raises_when_capture_cannot_open(self, mocker):
        mock_cap = mocker.Mock()
        mock_cap.isOpened.return_value = False
        mocker.patch("cv2.VideoCapture", return_value=mock_cap)

        with pytest.raises(RenderError, match="Cannot open"):
            reframe.reframe("bad.mp4", mocker.Mock(), mocker.Mock())

    def test_raises_on_invalid_dimensions(self, mocker):
        import cv2

        mock_cap = mocker.Mock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FRAME_WIDTH: 0,
            cv2.CAP_PROP_FRAME_HEIGHT: 0,
            cv2.CAP_PROP_FPS: 30,
            cv2.CAP_PROP_FRAME_COUNT: 100,
        }.get(prop, 0)
        mocker.patch("cv2.VideoCapture", return_value=mock_cap)

        with pytest.raises(RenderError, match="invalid dimensions"):
            reframe.reframe("bad.mp4", mocker.Mock(), mocker.Mock())
