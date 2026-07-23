"""Stage 5 — Reframe to 9:16 with speaker tracking.

Strategy: detect the speaker's face every Nth frame (FACE_DETECT_STRIDE —
the trajectory is smoothed anyway, so full per-frame detection buys nothing
but 5-10x more MediaPipe calls), interpolate between detections, apply an
exponential moving average for cinematic camera motion, then render a 9:16
crop that follows the smoothed centre. Audio is muxed back in with FFmpeg
(OpenCV writes video only).

Backends (TRACKING_BACKEND):
  mediapipe : MediaPipe face detection (best)
  opencv    : Haar cascade (no extra model download)
  center    : static centre crop (fastest, no tracking)

All OpenCV resources (VideoCapture/VideoWriter) are released in `finally`
blocks — a mid-loop exception (corrupt frame, disk full, OOM) used to leak
file handles that accumulated across a day of unattended batch processing
until the worker process ran out of descriptors.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.core.config import settings
from app.core.exceptions import RenderError
from app.core.logging import logger
from app.pipeline import ffmpeg_utils


def reframe(src: str | Path, dst: Path, work: Path) -> Path:
    src = str(src)
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RenderError(f"Cannot open {src} for reframing")

    try:
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or settings.target_fps
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if src_w <= 0 or src_h <= 0:
            raise RenderError(f"{src} reports invalid dimensions {src_w}x{src_h}")
        if n_frames <= 0:
            # Some containers don't report a reliable frame count; fall back
            # to a large upper bound and let the read loop terminate naturally.
            logger.warning("{} reports no frame count — proceeding without one", src)
            n_frames = 0

        out_w, out_h = settings.target_width, settings.target_height

        # Crop window dimensions inside the source that yield a 9:16 aspect.
        crop_h = src_h
        crop_w = int(round(crop_h * out_w / out_h))
        if crop_w > src_w:  # source not wide enough — clamp and take full width
            crop_w = src_w
            crop_h = int(round(crop_w * out_h / out_w))

        logger.info(
            "Reframe {}x{} -> crop {}x{} -> {}x{} ({} frames, backend={}, stride={})",
            src_w, src_h, crop_w, crop_h, out_w, out_h, n_frames,
            settings.tracking_backend, settings.face_detect_stride,
        )

        centers = _track_centers(cap, src_w, n_frames)
        smoothed = _smooth(centers, src_w // 2)

        # Rewinding a compressed video with CAP_PROP_POS_FRAMES can drift on
        # streams with long GOPs/B-frames. Re-opening a fresh capture handle
        # is slightly slower but frame-accurate on every container we've
        # tested against, and our inputs are always our own re-encoded
        # (libx264, short GOP) segments, so the cost is small.
        cap.release()
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            raise RenderError(f"Cannot re-open {src} for rendering pass")

        silent = work / "reframed_silent.mp4"
        writer = cv2.VideoWriter(
            str(silent), cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, out_h)
        )
        if not writer.isOpened():
            raise RenderError(f"Could not open VideoWriter for {silent}")

        try:
            max_x = max(src_w - crop_w, 0)
            idx = 0
            frames_written = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                cx = smoothed[idx] if idx < len(smoothed) else src_w // 2
                x0 = int(np.clip(cx - crop_w / 2, 0, max_x))
                y0 = int(np.clip((src_h - crop_h) / 2, 0, max(src_h - crop_h, 0)))
                crop = frame[y0 : y0 + crop_h, x0 : x0 + crop_w]
                if crop.size == 0:
                    idx += 1
                    continue
                resized = cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_AREA)
                writer.write(resized)
                frames_written += 1
                idx += 1
        finally:
            writer.release()

        if frames_written == 0:
            raise RenderError(f"Reframe produced zero output frames from {src}")

    finally:
        cap.release()

    # Mux original audio back in (hardware-encoder-aware, with automatic
    # libx264 fallback if the GPU encoder isn't actually usable).
    ffmpeg_utils.mux_video_audio(silent, src, dst)
    silent.unlink(missing_ok=True)
    return dst


# --- Tracking backends --------------------------------------------------------

def _track_centers(cap, src_w: int, n_frames: int) -> list[float | None]:
    backend = settings.tracking_backend
    if backend == "center":
        return [src_w / 2] * max(n_frames, 1)
    if backend == "opencv":
        return _track_strided(cap, lambda frame: _detect_opencv(frame, _opencv_cascade()))
    return _track_strided(cap, lambda frame: _detect_mediapipe(frame, src_w))


_mp_detector = None


def _mediapipe_detector():
    """Lazily build (and cache) one MediaPipe detector per process instead of
    per clip — model load is not free and this function runs per clip."""
    global _mp_detector
    if _mp_detector is None:
        import mediapipe as mp

        _mp_detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5
        )
    return _mp_detector


def _detect_mediapipe(frame, src_w: int) -> float | None:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    res = _mediapipe_detector().process(rgb)
    if not res.detections:
        return None
    best = max(
        res.detections, key=lambda d: d.location_data.relative_bounding_box.width
    )
    box = best.location_data.relative_bounding_box
    return (box.xmin + box.width / 2) * src_w


_cascade = None


def _opencv_cascade():
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _cascade


def _detect_opencv(frame, cascade) -> float | None:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return x + w / 2


def _track_strided(cap, detect_fn) -> list[float | None]:
    """Run `detect_fn(frame)` only every FACE_DETECT_STRIDE frames; frames in
    between reuse the previous detection result. This is the main reframe.py
    performance lever — MediaPipe inference dominates the stage's wall clock,
    and a smoothed trajectory doesn't need a fresh detection every frame."""
    stride = max(1, settings.face_detect_stride)
    centers: list[float | None] = []
    idx = 0
    last: float | None = None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            last = detect_fn(frame)
        centers.append(last)
        idx += 1
    return centers


# --- Trajectory smoothing -----------------------------------------------------

def _smooth(centers: list[float | None], default: float, alpha: float = 0.12) -> list[float]:
    """Fill gaps (no face) with the last known centre, then apply an
    exponential moving average for cinematic, jitter-free motion."""
    filled: list[float] = []
    last = default
    for c in centers:
        last = c if c is not None else last
        filled.append(last)

    out: list[float] = []
    ema = filled[0] if filled else default
    for x in filled:
        ema = alpha * x + (1 - alpha) * ema
        out.append(ema)
    return out
