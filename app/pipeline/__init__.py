"""Pipeline stages. Each module exposes a single pure-ish function that the
orchestrator chains together:

    ingest      -> local mp4 path + metadata
    transcribe  -> word-level transcript (Whisper)
    analyze     -> ranked list of clip candidates (Claude)
    cut         -> trimmed source segment per clip (FFmpeg)
    reframe     -> 9:16 speaker-tracked video (OpenCV/MediaPipe + FFmpeg)
    subtitles   -> animated captions burned in (FFmpeg ASS)
    metadata    -> title / hook / description / hashtags (Claude)

Every stage is independently testable and swappable — that is the whole point
of the modular design.
"""
