from __future__ import annotations

import bisect
import unicodedata
from collections.abc import Iterable
from itertools import pairwise
from typing import Any


_IGNORED_CATEGORIES = {"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Zl", "Zp", "Zs"}


def normalize_reading_text(value: str) -> str:
    """Normalize LN/STT text while preserving every spoken letter and digit."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and unicodedata.category(character) not in _IGNORED_CATEGORIES
    )


def _transcript_clock(segments: Iterable[dict[str, Any]]) -> tuple[str, list[float]]:
    text_parts: list[str] = []
    times: list[float] = []
    last_end = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        words = segment.get("words")
        rows = words if isinstance(words, list) and words else [segment]
        for row in rows:
            if not isinstance(row, dict):
                continue
            surface = str(row.get("word") or row.get("text") or "")
            normalized = normalize_reading_text(surface)
            if not normalized:
                continue
            try:
                start = max(last_end, float(row.get("start") or segment.get("start") or last_end))
                end = max(start + 0.02, float(row.get("end") or segment.get("end") or start + 0.02))
            except (TypeError, ValueError):
                continue
            for index, character in enumerate(normalized):
                text_parts.append(character)
                times.append(start + (end - start) * index / max(1, len(normalized)))
            last_end = end
    times.append(last_end)
    return "".join(text_parts), times


def _anchor_chain(
    novel: str,
    transcript: str,
    *,
    size: int,
    maximum_occurrences: int = 4,
) -> list[tuple[int, int]]:
    if len(novel) < size or len(transcript) < size:
        return []
    transcript_index: dict[str, list[int]] = {}
    for index in range(len(transcript) - size + 1):
        key = transcript[index : index + size]
        rows = transcript_index.setdefault(key, [])
        if len(rows) <= maximum_occurrences:
            rows.append(index)

    candidates: list[tuple[int, int]] = []
    for novel_index in range(len(novel) - size + 1):
        matches = transcript_index.get(novel[novel_index : novel_index + size]) or []
        if 0 < len(matches) <= maximum_occurrences:
            candidates.extend((novel_index, transcript_index_) for transcript_index_ in matches)
    if not candidates:
        return []
    # Descending transcript positions for equal novel positions prevent the
    # LIS from selecting several alternative matches for the same LN n-gram.
    candidates.sort(key=lambda item: (item[0], -item[1]))

    tails: list[int] = []
    tails_rows: list[int] = []
    previous = [-1] * len(candidates)
    for row_index, (_novel_index, transcript_index_) in enumerate(candidates):
        position = bisect.bisect_left(tails, transcript_index_)
        if position == len(tails):
            tails.append(transcript_index_)
            tails_rows.append(row_index)
        else:
            tails[position] = transcript_index_
            tails_rows[position] = row_index
        if position:
            previous[row_index] = tails_rows[position - 1]

    chain: list[tuple[int, int]] = []
    cursor = tails_rows[-1]
    while cursor >= 0:
        chain.append(candidates[cursor])
        cursor = previous[cursor]
    chain.reverse()

    # Keep almost every monotonic match.  Older builds kept one point per three
    # normalized characters.  That was enough to identify a chapter but could
    # drift visibly inside long sentences and around a chapter transition.
    # One point per two characters is still small compared with the cached STT
    # result and gives seeking/highlighting a substantially denser clock.
    compact: list[tuple[int, int]] = []
    for novel_index, transcript_index_ in chain:
        if compact and (
            novel_index <= compact[-1][0]
            or transcript_index_ <= compact[-1][1]
        ):
            continue
        if compact and novel_index - compact[-1][0] < 2:
            continue
        compact.append((novel_index, transcript_index_))
    return compact


def _best_dense_anchor_chain(novel: str, transcript: str) -> tuple[int, list[tuple[int, int]]]:
    """Return the densest reliable monotonic clock shared by LN and STT.

    Longer n-grams establish an unambiguous backbone.  A shorter chain is used
    only when it agrees with the same broad start/end corridor, which prevents
    common Japanese particles from creating a plausible but wrong timeline.
    """

    candidates: list[tuple[int, list[tuple[int, int]]]] = []
    for size, occurrences in ((11, 8), (9, 6), (7, 4), (5, 3), (4, 2)):
        chain = _anchor_chain(
            novel,
            transcript,
            size=size,
            maximum_occurrences=occurrences,
        )
        if len(chain) >= 8:
            candidates.append((size, chain))
    if not candidates:
        return 0, []

    backbone_size, backbone = candidates[0]
    best_size, best = backbone_size, backbone
    backbone_novel_span = max(1, backbone[-1][0] - backbone[0][0])
    backbone_audio_span = max(1, backbone[-1][1] - backbone[0][1])
    for size, chain in candidates[1:]:
        novel_overlap = min(backbone[-1][0], chain[-1][0]) - max(backbone[0][0], chain[0][0])
        audio_overlap = min(backbone[-1][1], chain[-1][1]) - max(backbone[0][1], chain[0][1])
        if novel_overlap < backbone_novel_span * 0.65 or audio_overlap < backbone_audio_span * 0.65:
            continue
        if len(chain) > len(best):
            best_size, best = size, chain
    return best_size, best


def _clip_activity_regions(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
) -> list[dict[str, float]]:
    clipped: list[dict[str, float]] = []
    for row in regions:
        if not isinstance(row, dict):
            continue
        try:
            region_start = max(float(start), float(row.get("start") or 0.0))
            region_end = min(float(end), float(row.get("end") or 0.0))
        except (TypeError, ValueError):
            continue
        if region_end - region_start >= 0.015:
            clipped.append(
                {"start": round(region_start, 3), "end": round(region_end, 3)}
            )
    return clipped


def _activity_ratio(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
    position: float,
) -> float:
    """Measure progress in spoken time, holding still through quiet gaps."""

    start = float(start)
    end = max(start + 0.02, float(end))
    position = max(start, min(end, float(position)))
    clipped = _clip_activity_regions(regions, start, end)
    total = sum(float(row["end"]) - float(row["start"]) for row in clipped)
    if total < max(0.04, (end - start) * 0.025):
        return (position - start) / (end - start)
    elapsed = 0.0
    for row in clipped:
        region_start = float(row["start"])
        region_end = float(row["end"])
        if position <= region_start:
            break
        elapsed += max(0.0, min(position, region_end) - region_start)
        if position < region_end:
            break
    return max(0.0, min(1.0, elapsed / total))


def _time_for_activity_ratio(
    regions: Iterable[dict[str, Any]],
    start: float,
    end: float,
    ratio: float,
) -> float:
    start = float(start)
    end = max(start + 0.02, float(end))
    ratio = max(0.0, min(1.0, float(ratio)))
    clipped = _clip_activity_regions(regions, start, end)
    total = sum(float(row["end"]) - float(row["start"]) for row in clipped)
    if total < max(0.04, (end - start) * 0.025):
        return start + ratio * (end - start)
    remaining = ratio * total
    for row in clipped:
        region_start = float(row["start"])
        length = float(row["end"]) - region_start
        if remaining < length - 1e-6:
            return region_start + remaining
        remaining -= length
    return float(clipped[-1]["end"])


def _acoustic_chapter_boundary(
    left_time: float,
    right_time: float,
    regions: Iterable[dict[str, Any]],
) -> float:
    """Prefer the next phrase onset after the longest chapter-sized pause."""

    fallback = (float(left_time) + float(right_time)) / 2
    clipped = _clip_activity_regions(regions, float(left_time), float(right_time))
    if len(clipped) < 2:
        return fallback
    gaps = [
        (float(right["start"]) - float(left["end"]), float(right["start"]))
        for left, right in pairwise(clipped)
    ]
    gap, next_onset = max(gaps, default=(0.0, fallback))
    return next_onset if gap >= 0.18 else fallback


def align_light_novel_to_transcript(
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    duration: float,
    model: str,
    speech_regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    transcript, transcript_times = _transcript_clock(segments)
    chapter_ranges: list[dict[str, Any]] = []
    novel_parts: list[str] = []
    cursor = 0
    for chapter in chapters:
        normalized = normalize_reading_text(str(chapter.get("text") or ""))
        if len(normalized) < 12:
            continue
        start = cursor
        novel_parts.append(normalized)
        cursor += len(normalized)
        chapter_ranges.append(
            {
                "chapter_index": int(chapter["chapter_index"]),
                "title": str(chapter.get("title") or ""),
                "global_start": start,
                "global_end": cursor,
                "length": len(normalized),
            }
        )
    novel = "".join(novel_parts)
    if len(transcript) < 40 or len(novel) < 40:
        raise ValueError("STT produced too little Japanese text for alignment")

    anchor_size, anchors = _best_dense_anchor_chain(novel, transcript)
    if len(anchors) < 8:
        raise ValueError("Not enough matching STT/LN anchors")

    aligned: list[dict[str, Any]] = []
    for chapter in chapter_ranges:
        local = [
            (novel_index - chapter["global_start"], transcript_index_)
            for novel_index, transcript_index_ in anchors
            if chapter["global_start"] <= novel_index < chapter["global_end"]
        ]
        if len(local) < 2:
            continue
        span = local[-1][0] - local[0][0] + anchor_size
        if len(local) < 4 and span < chapter["length"] * 0.20:
            continue
        clock = [
            {
                "offset": int(offset),
                "time": round(float(transcript_times[min(transcript_index_, len(transcript_times) - 1)]), 3),
            }
            for offset, transcript_index_ in local
        ]
        first_time = float(clock[0]["time"])
        last_time = float(clock[-1]["time"])
        character_rate = (
            max(0.25, (local[-1][0] - local[0][0]) / max(0.25, last_time - first_time))
            if len(local) > 1
            else 5.0
        )
        estimated_start = max(0.0, first_time - local[0][0] / character_rate)
        estimated_end = min(
            max(estimated_start + 0.25, float(duration)),
            last_time + max(0, chapter["length"] - local[-1][0]) / character_rate,
        )
        aligned.append(
            {
                "chapter_index": chapter["chapter_index"],
                "title": chapter["title"],
                "normalized_length": chapter["length"],
                "first_anchor_time": first_time,
                "last_anchor_time": last_time,
                "estimated_start": estimated_start,
                "estimated_end": estimated_end,
                "anchors": clock,
                "confidence": round(min(1.0, span / max(1, chapter["length"])), 4),
            }
        )
    if not aligned:
        raise ValueError("STT text did not match any readable LN chapter")

    for index, chapter in enumerate(aligned):
        if index:
            previous = aligned[index - 1]
            boundary = _acoustic_chapter_boundary(
                float(previous["last_anchor_time"]),
                float(chapter["first_anchor_time"]),
                speech_regions or [],
            )
            previous["end"] = round(max(float(previous["start"]) + 0.25, boundary), 3)
            chapter["start"] = round(max(0.0, boundary), 3)
        else:
            chapter["start"] = round(float(chapter["estimated_start"]), 3)
    aligned[-1]["end"] = round(
        min(max(float(aligned[-1]["start"]) + 0.25, float(duration)), float(aligned[-1]["estimated_end"])),
        3,
    )
    for chapter in aligned:
        chapter.setdefault("end", round(float(chapter["estimated_end"]), 3))
        chapter["anchors"] = [
            {"offset": 0, "time": chapter["start"]},
            *chapter["anchors"],
            {"offset": chapter["normalized_length"], "time": chapter["end"]},
        ]
        chapter["speech_regions"] = _clip_activity_regions(
            speech_regions or [],
            float(chapter["start"]),
            float(chapter["end"]),
        )
        for key in ("first_anchor_time", "last_anchor_time", "estimated_start", "estimated_end"):
            chapter.pop(key, None)

    return {
        "schema": "reading-audio-v3",
        "model": str(model),
        "duration": round(float(duration), 3),
        "transcript_characters": len(transcript),
        "novel_characters": len(novel),
        "anchor_count": len(anchors),
        "anchor_size": anchor_size,
        "chapters": aligned,
        "confidence": round(sum(float(row["confidence"]) for row in aligned) / len(aligned), 4),
    }


def audio_position_for_light_novel(
    alignment: dict[str, Any], chapter_index: int, progress: float
) -> float | None:
    chapter = next(
        (
            row
            for row in alignment.get("chapters") or []
            if int(row.get("chapter_index") or 0) == int(chapter_index)
        ),
        None,
    )
    if not isinstance(chapter, dict):
        return None
    target = max(0.0, min(1.0, float(progress))) * max(1, int(chapter["normalized_length"]))
    anchors = list(chapter.get("anchors") or [])
    speech_regions = list(chapter.get("speech_regions") or [])
    for left, right in pairwise(anchors):
        left_offset, right_offset = float(left["offset"]), float(right["offset"])
        if target <= right_offset:
            ratio = (target - left_offset) / max(1.0, right_offset - left_offset)
            return _time_for_activity_ratio(
                speech_regions,
                float(left["time"]),
                float(right["time"]),
                ratio,
            )
    return float(chapter["end"])


def audio_position_for_light_novel_offset(
    alignment: dict[str, Any], chapter_index: int, character_offset: int
) -> float | None:
    """Resolve a normalized LN character offset on the acoustic speech clock."""

    chapter = next(
        (
            row
            for row in alignment.get("chapters") or []
            if int(row.get("chapter_index") or 0) == int(chapter_index)
        ),
        None,
    )
    if not isinstance(chapter, dict):
        return None
    anchors = [row for row in chapter.get("anchors") or [] if isinstance(row, dict)]
    if not anchors:
        return None
    target = max(0.0, min(float(character_offset), float(chapter.get("normalized_length") or 0)))
    speech_regions = list(chapter.get("speech_regions") or [])
    for left, right in pairwise(anchors):
        left_offset = float(left.get("offset") or 0.0)
        right_offset = float(right.get("offset") or left_offset)
        if target <= right_offset:
            ratio = (target - left_offset) / max(1.0, right_offset - left_offset)
            return _time_for_activity_ratio(
                speech_regions,
                float(left.get("time") or 0.0),
                float(right.get("time") or 0.0),
                ratio,
            )
    return float(anchors[-1].get("time") or chapter.get("end") or 0.0)


def light_novel_position_for_audio(
    alignment: dict[str, Any], position: float
) -> dict[str, Any] | None:
    chapters = [row for row in alignment.get("chapters") or [] if isinstance(row, dict)]
    if not chapters:
        return None
    chapter = next(
        (
            row
            for row in chapters
            if float(row["start"]) <= float(position) < float(row["end"])
        ),
        min(
            chapters,
            key=lambda row: min(
                abs(float(position) - float(row["start"])),
                abs(float(position) - float(row["end"])),
            ),
        ),
    )
    anchors = [row for row in chapter.get("anchors") or [] if isinstance(row, dict)]
    if not anchors:
        return None
    target_offset = 0.0
    left_anchor = anchors[0]
    right_anchor = anchors[0]
    speech_regions = list(chapter.get("speech_regions") or [])
    for left, right in pairwise(anchors):
        if float(position) <= float(right["time"]):
            ratio = _activity_ratio(
                speech_regions,
                float(left["time"]),
                float(right["time"]),
                float(position),
            )
            target_offset = float(left["offset"]) + max(0.0, min(1.0, ratio)) * (float(right["offset"]) - float(left["offset"]))
            left_anchor = left
            right_anchor = right
            break
    else:
        target_offset = float(chapter["normalized_length"])
        if len(anchors) > 1:
            left_anchor, right_anchor = anchors[-2], anchors[-1]
    length = max(1, int(chapter["normalized_length"]))
    anchor_window: dict[str, Any] = {
        "left_time": float(left_anchor["time"]),
        "left_offset": float(left_anchor["offset"]),
        "right_time": float(right_anchor["time"]),
        "right_offset": float(right_anchor["offset"]),
    }
    window_activity = _clip_activity_regions(
        speech_regions,
        float(left_anchor["time"]),
        float(right_anchor["time"]),
    )
    if window_activity:
        anchor_window["activity"] = window_activity
    return {
        "chapter_index": int(chapter["chapter_index"]),
        "chapter_progress": max(0.0, min(1.0, target_offset / length)),
        "chapter_char_offset": int(round(target_offset)),
        "chapter_char_offset_exact": round(target_offset, 4),
        "chapter_char_count": length,
        "anchor_window": anchor_window,
    }
