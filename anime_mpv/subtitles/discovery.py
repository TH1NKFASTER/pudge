from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..models import SubtitleCandidate
from ..subtitle_formats import convert_to_plain_srt, parse_srt


def _content_fingerprint(
    candidate: SubtitleCandidate,
    cache_dir: Path,
    *,
    ffmpeg_path: str,
) -> str | None:
    path = candidate.path
    if path.suffix.casefold() in {".ass", ".ssa"}:
        path, _ = convert_to_plain_srt(
            path,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=False,
            verbose=False,
        )
    if path.suffix.casefold() != ".srt":
        return None
    try:
        cues = parse_srt(path)
    except OSError:
        return None
    if not cues:
        return None
    normalized = []
    for start, end, text in cues:
        clean_text = re.sub(r"\s+", "", text).casefold()
        clean_text = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", clean_text)
        normalized.append(f"{round(start, 2):.2f}|{round(end, 2):.2f}|{clean_text}")
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def deduplicate_candidates(
    candidates: list[SubtitleCandidate],
    cache_dir: Path,
    *,
    ffmpeg_path: str,
) -> tuple[list[SubtitleCandidate], int]:
    """Collapse duplicate paths and equivalent SRT/ASS contents."""
    by_path: dict[str, SubtitleCandidate] = {}
    for candidate in candidates:
        key = str(candidate.path.resolve())
        current = by_path.get(key)
        if current is None or candidate.score > current.score:
            by_path[key] = candidate

    selected: dict[str, SubtitleCandidate] = {}
    passthrough: list[SubtitleCandidate] = []
    for candidate in by_path.values():
        fingerprint = _content_fingerprint(candidate, cache_dir, ffmpeg_path=ffmpeg_path)
        if fingerprint is None:
            passthrough.append(candidate)
            continue
        current = selected.get(fingerprint)
        if current is None or (
            int(candidate.path.suffix.casefold() == ".srt"), candidate.score
        ) > (int(current.path.suffix.casefold() == ".srt"), current.score):
            selected[fingerprint] = candidate

    result = sorted(
        [*selected.values(), *passthrough],
        key=lambda item: item.score,
        reverse=True,
    )
    return result, max(0, len(candidates) - len(result))
