"""Stage 7 — Generate publishing metadata with Claude.

For each selected clip: a scroll-stopping title, an on-screen hook, a platform
description, and hashtags. One call per clip keeps each focused and cheap; the
orchestrator can fan these out in parallel.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.pipeline import claude_client


class ClipMetadata(BaseModel):
    title: str = Field(description="Punchy title, <= 80 chars, no clickbait lies")
    hook: str = Field(description="First-frame on-screen text, <= 60 chars")
    description: str = Field(description="1-2 sentence caption for the post")
    hashtags: list[str] = Field(description="6-10 relevant hashtags without '#'")


SYSTEM = """You write metadata for short-form vertical videos (TikTok, Reels, \
Shorts). Given a clip transcript, produce:
- title: a punchy, curiosity-driven title (<=80 chars). Accurate, not clickbait.
- hook: <=60 chars of on-screen text for the first frame that stops the scroll.
- description: a 1-2 sentence caption.
- hashtags: 6-10 relevant, discoverable hashtags (lowercase, no '#' prefix, no spaces).
Match the tone and topic of the clip. Do not invent facts not in the transcript."""


def generate(clip_transcript: str, context_title: str = "") -> ClipMetadata:
    user = (
        f"Source video: {context_title}\n\n" if context_title else ""
    ) + f"CLIP TRANSCRIPT:\n{clip_transcript}"
    meta = claude_client.parse(
        system=SYSTEM, user=user, schema=ClipMetadata, max_tokens=2000, purpose="metadata"
    )
    meta.hashtags = [h.lstrip("#").strip().replace(" ", "") for h in meta.hashtags if h.strip()]
    return meta
