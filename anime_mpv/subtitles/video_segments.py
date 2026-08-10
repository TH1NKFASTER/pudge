from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


def probe_container_edit_points(video: Path, ffprobe_path: str = "ffprobe") -> list[dict[str, object]]:
    """Return chapter boundaries carried by MKV/MP4 as cheap edit hints."""
    binary = shutil.which(ffprobe_path) if "/" not in ffprobe_path else ffprobe_path
    if not binary or not Path(binary).is_file():
        return []
    command = [
        binary,
        "-v",
        "error",
        "-show_chapters",
        "-of",
        "json",
        str(video),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout or "{}") if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return []
    points: list[dict[str, object]] = []
    for chapter in payload.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        tags = chapter.get("tags") if isinstance(chapter.get("tags"), dict) else {}
        title = str(tags.get("title") or tags.get("TITLE") or "").strip()
        for key, kind in (("start_time", "chapter_start"), ("end_time", "chapter_end")):
            try:
                seconds = float(chapter.get(key))
            except (TypeError, ValueError):
                continue
            if seconds <= 0:
                continue
            points.append({"time": round(seconds, 3), "kind": kind, "title": title})
    unique: dict[int, dict[str, object]] = {}
    for point in points:
        unique.setdefault(int(round(float(point["time"]) * 10)), point)
    return sorted(unique.values(), key=lambda item: float(item["time"]))


def choose_edit_boundary(
    left_time: float,
    right_time: float,
    *,
    midpoint: float,
    edit_points: Iterable[dict[str, object]] = (),
    cue_gaps: Iterable[tuple[float, float, float]] = (),
) -> tuple[float, dict[str, object]]:
    """Choose a real chapter/caption break instead of an arbitrary midpoint."""
    candidates: list[tuple[float, float, dict[str, object]]] = []
    span = max(1.0, right_time - left_time)
    for point in edit_points:
        try:
            value = float(point.get("time"))
        except (AttributeError, TypeError, ValueError):
            continue
        if left_time <= value <= right_time:
            title = str(point.get("title") or "").casefold()
            named_bonus = 0.25 if any(
                token in title
                for token in ("op", "opening", "ed", "ending", "intro", "chapter", "part")
            ) else 0.0
            distance = abs(value - midpoint) / span
            candidates.append((1.2 + named_bonus - distance, value, dict(point)))
    for gap_start, gap_end, gap_seconds in cue_gaps:
        value = float(gap_end)
        if left_time <= value <= right_time:
            distance = abs(value - midpoint) / span
            candidates.append(
                (
                    1.0 + min(0.5, float(gap_seconds) / 120.0) - distance,
                    value,
                    {"kind": "subtitle_gap", "gap_seconds": round(float(gap_seconds), 3)},
                )
            )
    if not candidates:
        return midpoint, {"kind": "midpoint"}
    _score, value, evidence = max(candidates, key=lambda item: item[0])
    return value, evidence
