"""Stage 3 — Analyze transcript with Claude and select the best moments.

Claude is given the timestamped transcript and asked to return 10-15 ranked
clip candidates optimised for short-form virality. We then *snap* each returned
timestamp to the nearest word boundary so cuts never slice mid-word, and clamp
durations to the configured min/max.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import logger
from app.pipeline import claude_client

# --- Structured output schema -------------------------------------------------

VALID_CATEGORIES = ["hook", "emotional", "informative", "funny", "viral"]


class ClipCandidate(BaseModel):
    start_seconds: float = Field(description="Clip start time in seconds")
    end_seconds: float = Field(description="Clip end time in seconds")
    score: float = Field(description="Viral potential 0-100")
    title_hint: str = Field(description="Short label for the moment")
    reason: str = Field(description="Why this moment is engaging")
    categories: list[str] = Field(description="Any of: hook, emotional, informative, funny, viral")
    transcript_text: str = Field(description="The spoken words in this clip")


class AnalysisResult(BaseModel):
    clips: list[ClipCandidate]


SYSTEM = """You are an expert short-form video producer who has cut thousands of \
viral TikTok, Reels and Shorts clips from long-form content.

Given a timestamped transcript, identify the most engaging self-contained \
moments to turn into vertical clips. Judge each candidate on:
- Strong hook (grabs attention in the first 2 seconds)
- Emotional impact (surprise, inspiration, tension, humor)
- Valuable/quotable information
- Funny or unexpected moments
- Overall viral potential and shareability

Rules:
- Each clip must be a complete thought with a clean start and end — never cut \
mid-sentence.
- Prefer moments that make sense without external context.
- Duration must be between {min_s} and {max_s} seconds.
- Return between {min_n} and {max_n} clips, ranked best-first by score.
- Timestamps must fall within the transcript's time range.
- categories must only contain values from: hook, emotional, informative, funny, viral.
- Write title_hint and reason in English, regardless of the transcript's \
original language — the target audience is English-speaking.
"""


def _transcript_for_prompt(transcript: dict) -> str:
    """Compact segment listing: `[start-end] text`. Keeps token use reasonable
    on long videos while preserving the timing Claude needs."""
    lines = []
    for s in transcript["segments"]:
        lines.append(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    return "\n".join(lines)


def analyze(transcript: dict) -> list[ClipCandidate]:
    system = SYSTEM.format(
        min_s=settings.min_clip_seconds,
        max_s=settings.max_clip_seconds,
        min_n=settings.min_clips_per_video,
        max_n=settings.max_clips_per_video,
    )
    user = (
        f"Video duration: {transcript['duration']:.0f}s\n"
        f"Language: {transcript.get('language', 'unknown')}\n\n"
        f"TRANSCRIPT:\n{_transcript_for_prompt(transcript)}"
    )

    result = claude_client.parse(
        system=system, user=user, schema=AnalysisResult, max_tokens=12000, purpose="analyze"
    )
    logger.info("Claude proposed {} clip candidates", len(result.clips))

    cleaned = _post_process(result.clips, transcript)
    logger.info("{} candidates after snapping/clamping", len(cleaned))
    return cleaned


def _all_words(transcript: dict) -> list[dict]:
    words: list[dict] = []
    for seg in transcript["segments"]:
        words.extend(seg.get("words") or [])
    return words


def _snap_to_word(t: float, words: list[dict], edge: str) -> float:
    """Snap a timestamp to the nearest word boundary so cuts land on silence."""
    if not words:
        return t
    cands = [w["start"] for w in words] if edge == "start" else [w["end"] for w in words]
    return min(cands, key=lambda x: abs(x - t))


def _post_process(candidates: list[ClipCandidate], transcript: dict) -> list[ClipCandidate]:
    words = _all_words(transcript)
    duration = transcript["duration"]
    out: list[ClipCandidate] = []

    for c in candidates:
        start = max(0.0, min(c.start_seconds, duration))
        end = max(0.0, min(c.end_seconds, duration))
        if end <= start:
            continue
        start = _snap_to_word(start, words, "start")
        end = _snap_to_word(end, words, "end")

        dur = end - start
        if dur < settings.min_clip_seconds:
            continue  # too short even after snapping
        if dur > settings.max_clip_seconds:
            end = start + settings.max_clip_seconds

        c.start_seconds = round(start, 3)
        c.end_seconds = round(end, 3)
        c.categories = [x for x in c.categories if x in VALID_CATEGORIES] or ["viral"]
        out.append(c)

    # Rank best-first, cap to configured maximum.
    out.sort(key=lambda x: x.score, reverse=True)
    return out[: settings.max_clips_per_video]
