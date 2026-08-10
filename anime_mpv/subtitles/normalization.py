from __future__ import annotations

from pathlib import Path

from ..subtitle_formats import convert_to_plain_srt, parse_srt


def normalize_text_subtitle(
    source: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Normalize text captions to the internal cue representation/SRT."""
    if source.suffix.casefold() in {".ass", ".ssa"}:
        return convert_to_plain_srt(
            source,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=False,
        )
    if source.suffix.casefold() == ".srt":
        cues = parse_srt(source)
        return source, {"reason": "already_srt", "cue_count": len(cues)}
    return source, {"reason": "unsupported_format", "cue_count": 0}
