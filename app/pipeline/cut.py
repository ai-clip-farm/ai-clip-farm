"""Stage 4 — Cut the selected segment from the source (thin wrapper)."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.ffmpeg_utils import cut_segment


def cut(src: str | Path, dst: Path, start: float, end: float) -> Path:
    return cut_segment(src, dst, start, end)
