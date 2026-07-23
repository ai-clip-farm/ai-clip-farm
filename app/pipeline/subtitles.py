"""Stage 6 — Animated subtitles.

Builds an ASS subtitle file from word-level timestamps and burns it into the
9:16 clip with FFmpeg. Words are grouped into short caption lines (max ~4
words) and animated word-by-word (karaoke highlight) — the look that performs
on TikTok/Reels/Shorts.

Style presets live in STYLE_PRESETS so adding a new caption look is a data
change, not a code change. Font names must match a font actually installed in
the container (see Dockerfile) — libass silently substitutes an arbitrary
fallback font (via fontconfig) if the requested family isn't found, which
looks wrong without ever raising an error, so we bundle the exact families we
reference (`Anton`, `Montserrat`) rather than assuming a system default.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import RenderError
from app.pipeline import ffmpeg_utils

# --- Style presets ------------------------------------------------------------
# Colours are ASS &HAABBGGRR (alpha, blue, green, red).
# `font` must be a family name actually installed in the container. We use
# only fonts pulled in by the `fonts-dejavu-core` / `fonts-liberation` apt
# packages in the Dockerfile — no build-time network font download, so a
# fully offline `docker build` still produces correct-looking subtitles.
# libass silently substitutes a fallback font (via fontconfig) if the
# requested family isn't found; that's a "wrong but no error" failure mode,
# so this list must stay in lockstep with the Dockerfile's installed fonts.
STYLE_PRESETS: dict[str, dict] = {
    "karaoke_bold": {
        "font": "DejaVu Sans",
        "fontsize": 110,
        "primary": "&H00FFFFFF",  # inactive words: white
        "highlight": "&H0000E5FF",  # active word: gold/yellow
        "outline": "&H00000000",
        "outline_w": 6,
        "shadow": 3,
        "bold": -1,
        "margin_v": 420,  # sit above centre, out of the lower third
        "words_per_line": 4,
    },
    "clean_white": {
        "font": "Liberation Sans",
        "fontsize": 92,
        "primary": "&H00FFFFFF",
        "highlight": "&H0000FFFF",
        "outline": "&H00202020",
        "outline_w": 4,
        "shadow": 2,
        "bold": -1,
        "margin_v": 480,
        "words_per_line": 3,
    },
}

# ASS override blocks use `{ }` and `\` as control syntax, and treats a
# literal newline as a hard line break. A transcribed word containing any of
# these (rare, but real — e.g. Whisper reading out code or math aloud) would
# otherwise corrupt the subtitle's styling or, worst case, get interpreted as
# an override tag. Escape defensively.
_ASS_SPECIAL_RE = re.compile(r"([{}\\])")


def _escape_ass_text(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    return _ASS_SPECIAL_RE.sub(r"\\\1", text)


def _fmt_time(t: float) -> str:
    """Seconds -> ASS h:mm:ss.cs."""
    cs = int(round(max(t, 0.0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clip_words(transcript: dict, start: float, end: float) -> list[dict]:
    """All words overlapping [start, end], rebased so the clip starts at 0."""
    out: list[dict] = []
    for seg in transcript.get("segments", []):
        for w in seg.get("words") or []:
            if w["end"] <= start or w["start"] >= end:
                continue
            out.append(
                {
                    "start": max(0.0, w["start"] - start),
                    "end": max(0.0, w["end"] - start),
                    "word": w["word"].strip(),
                }
            )
    return out


def _group_lines(words: list[dict], per_line: int) -> list[list[dict]]:
    return [words[i : i + per_line] for i in range(0, len(words), per_line)]


def build_ass(transcript: dict, start: float, end: float, dst: Path) -> Path:
    style = STYLE_PRESETS.get(settings.subtitle_style, STYLE_PRESETS["karaoke_bold"])
    words = _clip_words(transcript, start, end)
    lines = _group_lines(words, style["words_per_line"])

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {settings.target_width}
PlayResY: {settings.target_height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style['font']},{style['fontsize']},{style['primary']},{style['highlight']},{style['outline']},&H64000000,{style['bold']},0,0,0,100,100,0,0,1,{style['outline_w']},{style['shadow']},2,60,60,{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []
    for line in lines:
        if not line:
            continue
        l_start = line[0]["start"]
        l_end = line[-1]["end"]
        if l_end <= l_start:
            continue
        # Karaoke: each word highlights for its own duration (\k = centiseconds).
        chunks = []
        for w in line:
            dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            word = _escape_ass_text(w["word"])
            # {\kf} sweeps the highlight; SecondaryColour is the active colour.
            chunks.append(f"{{\\kf{dur_cs}}}{word} ")
        text = "".join(chunks).strip()
        # Pop-in scale animation for extra energy.
        text = f"{{\\fad(80,80)\\t(0,120,\\fscx110\\fscy110)\\t(120,240,\\fscx100\\fscy100)}}{text}"
        events.append(f"Dialogue: 0,{_fmt_time(l_start)},{_fmt_time(l_end)},Cap,,0,0,0,,{text}")

    if not events:
        raise RenderError("No transcript words found in the clip's time range")

    dst.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return dst


def burn(clip_path: Path, ass_path: Path, dst: Path) -> Path:
    """Burn the ASS subtitles into the video (hardware-encoder-aware)."""
    return ffmpeg_utils.burn_subtitles(clip_path, ass_path, dst)
